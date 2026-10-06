from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from job_scout.dedupe.resolver import delivery_keys, representative_key
from job_scout.domain.models import Job, JobLifecycle, JobMatch
from job_scout.retention import retention_basis


class CompatibleRow:
    """Tuple-like row with sqlite3.Row-style named access across SQLite drivers."""

    def __init__(self, cursor, values) -> None:
        self._values = tuple(values)
        self._index = {
            column[0]: index for index, column in enumerate(cursor.description or ())
        }

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._values[self._index[key]]
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def keys(self):
        return self._index.keys()


def compatible_row_factory(cursor, values) -> CompatibleRow:
    return CompatibleRow(cursor, values)


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  source_job_id TEXT NOT NULL,
  source_board_id TEXT NOT NULL,
  canonical_url TEXT NOT NULL,
  content_fingerprint TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  last_verified_at TEXT NOT NULL,
  lifecycle TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  UNIQUE(source, source_board_id, source_job_id)
);
CREATE TABLE IF NOT EXISTS job_matches (
  job_id TEXT NOT NULL REFERENCES jobs(id),
  client_id TEXT NOT NULL,
  decision TEXT NOT NULL,
  score INTEGER,
  matched_reasons_json TEXT NOT NULL,
  rejection_reasons_json TEXT NOT NULL,
  evaluated_at TEXT NOT NULL,
  matcher_version TEXT NOT NULL,
  PRIMARY KEY(job_id, client_id)
);
CREATE TABLE IF NOT EXISTS scoped_job_matches (
  job_id TEXT NOT NULL REFERENCES jobs(id),
  client_id TEXT NOT NULL,
  match_scope_id TEXT NOT NULL,
  decision TEXT NOT NULL,
  score INTEGER,
  matched_reasons_json TEXT NOT NULL,
  rejection_reasons_json TEXT NOT NULL,
  evaluated_at TEXT NOT NULL,
  matcher_version TEXT NOT NULL,
  PRIMARY KEY(job_id, client_id, match_scope_id)
);
CREATE INDEX IF NOT EXISTS ix_scoped_job_matches_client_scope
  ON scoped_job_matches(client_id, match_scope_id, job_id);
CREATE TABLE IF NOT EXISTS collection_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL, target TEXT NOT NULL, started_at TEXT NOT NULL,
  completed_at TEXT, status TEXT NOT NULL, metrics_json TEXT NOT NULL, errors_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exports (
  job_id TEXT NOT NULL REFERENCES jobs(id),
  client_id TEXT NOT NULL,
  destination TEXT NOT NULL,
  exported_at TEXT NOT NULL,
  PRIMARY KEY(job_id, client_id, destination)
);
CREATE TABLE IF NOT EXISTS delivery_groups (id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS posting_delivery_groups (
  job_id TEXT PRIMARY KEY REFERENCES jobs(id),
  group_id TEXT NOT NULL REFERENCES delivery_groups(id)
);
CREATE INDEX IF NOT EXISTS ix_posting_group ON posting_delivery_groups(group_id);
CREATE TABLE IF NOT EXISTS delivery_keys (
  job_id TEXT NOT NULL REFERENCES jobs(id), kind TEXT NOT NULL, value TEXT NOT NULL,
  PRIMARY KEY(job_id, kind, value)
);
CREATE INDEX IF NOT EXISTS ix_delivery_key ON delivery_keys(kind, value);
CREATE TABLE IF NOT EXISTS job_identity_ledger (
  source TEXT NOT NULL,
  source_board_id TEXT NOT NULL,
  source_job_id TEXT NOT NULL,
  canonical_url TEXT NOT NULL,
  employer_id TEXT,
  company TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  pruned_at TEXT NOT NULL,
  was_delivered INTEGER NOT NULL CHECK(was_delivered IN (0,1)),
  PRIMARY KEY(source, source_board_id, source_job_id)
);
CREATE TABLE IF NOT EXISTS job_retention_evidence (
  job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
  posted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS group_deliveries (
  group_id TEXT NOT NULL REFERENCES delivery_groups(id),
  client_id TEXT NOT NULL, destination TEXT NOT NULL,
  job_id TEXT NOT NULL REFERENCES jobs(id), exported_at TEXT NOT NULL,
  PRIMARY KEY(group_id, client_id, destination)
);
CREATE TABLE IF NOT EXISTS historical_imports (
  id TEXT PRIMARY KEY, client_id TEXT NOT NULL, workbook_sha256 TEXT NOT NULL,
  imported_at TEXT NOT NULL, importer_version TEXT NOT NULL, row_count INTEGER NOT NULL,
  UNIQUE(client_id, workbook_sha256)
);
CREATE TABLE IF NOT EXISTS historical_job_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT, import_id TEXT NOT NULL REFERENCES historical_imports(id),
  client_id TEXT NOT NULL, original_url TEXT NOT NULL, normalized_url TEXT NOT NULL,
  source TEXT, source_board_id TEXT, source_job_id TEXT, title TEXT NOT NULL, company TEXT NOT NULL,
  operator_status TEXT NOT NULL CHECK(operator_status IN ('applied','not_applied','unknown')),
  source_sheet TEXT NOT NULL, source_row INTEGER NOT NULL, imported_at TEXT NOT NULL,
  UNIQUE(import_id, source_sheet, source_row, original_url)
);
CREATE INDEX IF NOT EXISTS ix_historical_url ON historical_job_links(client_id, normalized_url);
CREATE INDEX IF NOT EXISTS ix_historical_identity ON historical_job_links(client_id, source, source_board_id, source_job_id);
CREATE TABLE IF NOT EXISTS destination_observed_links (
  client_id TEXT NOT NULL,
  destination TEXT NOT NULL,
  normalized_url TEXT NOT NULL,
  source TEXT,
  source_board_id TEXT,
  source_job_id TEXT,
  first_observed_at TEXT NOT NULL,
  PRIMARY KEY(client_id, destination, normalized_url)
);
CREATE INDEX IF NOT EXISTS ix_destination_observed_url
  ON destination_observed_links(client_id, normalized_url);
CREATE INDEX IF NOT EXISTS ix_destination_observed_identity
  ON destination_observed_links(client_id, source, source_board_id, source_job_id);
CREATE TABLE IF NOT EXISTS historical_blacklist_evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT, import_id TEXT NOT NULL REFERENCES historical_imports(id),
  client_id TEXT NOT NULL, value TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('company','note')),
  source_sheet TEXT NOT NULL, source_row INTEGER NOT NULL, imported_at TEXT NOT NULL,
  UNIQUE(import_id, source_sheet, source_row, value)
);
CREATE INDEX IF NOT EXISTS ix_historical_blacklist_client ON historical_blacklist_evidence(client_id);
"""


class SQLiteRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.remote_url = os.getenv("TURSO_DATABASE_URL", "").strip()
        self.auth_token = os.getenv("TURSO_AUTH_TOKEN", "").strip()
        if bool(self.remote_url) != bool(self.auth_token):
            raise ValueError(
                "TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be configured together"
            )
        self._turso_sync = None
        self._turso_error = ()
        if self.remote_url:
            try:
                import turso
                import turso.sync
            except ImportError as error:
                raise RuntimeError(
                    "install job-scout[cloud] for Turso-backed persistence"
                ) from error
            self._turso_sync = turso.sync
            self._turso_error = (turso.Error,)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError(
                    "database contains orphaned legacy rows; restore missing source evidence "
                    "before delivery-group migration (no records were deleted)"
                )
            connection.execute("DROP INDEX IF EXISTS uq_jobs_canonical_url")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_jobs_canonical_url ON jobs(canonical_url)"
            )
            # Backfill only postings lacking a group; safe to reopen repeatedly.
            for row in connection.execute(
                "SELECT j.payload_json FROM jobs j LEFT JOIN posting_delivery_groups g "
                "ON g.job_id=j.id WHERE g.job_id IS NULL ORDER BY j.id"
            ).fetchall():
                self._assign_group(connection, Job.model_validate_json(row[0]))
            connection.execute(
                "INSERT OR IGNORE INTO group_deliveries "
                "SELECT g.group_id, e.client_id, e.destination, e.job_id, e.exported_at "
                "FROM exports e JOIN posting_delivery_groups g ON g.job_id=e.job_id "
                "ORDER BY e.exported_at, e.job_id"
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            if self.remote_url:
                connection = self._turso_sync.connect(
                    self.path,
                    remote_url=self.remote_url,
                    auth_token=self.auth_token,
                )
            else:
                connection = sqlite3.connect(self.path)
            connection.row_factory = compatible_row_factory
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
            connection.commit()
            if self.remote_url:
                connection.push()
        except self._turso_error as error:
            raise sqlite3.DatabaseError(str(error)) from error
        finally:
            if connection is not None:
                connection.close()

    def _upsert_job_in_connection(
        self, connection: sqlite3.Connection, job: Job, now: str
    ) -> JobLifecycle:
        row = connection.execute(
            "SELECT id, content_fingerprint FROM jobs "
            "WHERE source=? AND source_board_id=? AND source_job_id=?",
            (job.source, job.source_board_id, job.source_job_id),
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job.id,
                    job.source,
                    job.source_job_id,
                    job.source_board_id,
                    str(job.canonical_url),
                    job.content_fingerprint,
                    job.discovered_at.isoformat(),
                    job.last_seen_at.isoformat(),
                    now,
                    JobLifecycle.NEW.value,
                    job.model_dump_json(),
                ),
            )
            connection.execute(
                "DELETE FROM job_retention_evidence WHERE job_id=?", (job.id,)
            )
            self._assign_group(connection, job)
            return JobLifecycle.NEW

        lifecycle = (
            JobLifecycle.CHANGED
            if row["content_fingerprint"] != job.content_fingerprint
            else JobLifecycle.SEEN
        )
        job.id = row["id"]
        connection.execute(
            "UPDATE jobs SET canonical_url=?, content_fingerprint=?, last_seen_at=?, "
            "last_verified_at=?, lifecycle=?, payload_json=? WHERE id=?",
            (
                str(job.canonical_url),
                job.content_fingerprint,
                job.last_seen_at.isoformat(),
                now,
                lifecycle.value,
                job.model_dump_json(),
                job.id,
            ),
        )
        connection.execute(
            "DELETE FROM job_retention_evidence WHERE job_id=?", (job.id,)
        )
        self._assign_group(connection, job)
        return lifecycle

    def _upsert_jobs_in_connection(
        self,
        connection: sqlite3.Connection,
        jobs: Iterable[Job],
        now: str,
    ) -> dict[str, JobLifecycle]:
        """Persist a batch using one caller-owned transaction.

        SQLite keeps the proven row-at-a-time semantics. Production Postgres
        overrides this hook with a set-oriented implementation so large fan-in
        payloads do not pay one network round trip per posting.
        """
        states: dict[str, JobLifecycle] = {}
        for job in jobs:
            state = self._upsert_job_in_connection(connection, job, now)
            states[job.id] = state
        return states

    def upsert_job(self, job: Job) -> JobLifecycle:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._upsert_job_in_connection(connection, job, now)

    def upsert_jobs(self, jobs: Iterable[Job]) -> dict[str, JobLifecycle]:
        """Persist one provider batch in a single transaction/push."""
        values = list(jobs)
        if not values:
            return {}
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._upsert_jobs_in_connection(connection, values, now)

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def _record_pruned_identities_in_connection(
        self,
        connection: sqlite3.Connection,
        jobs: Iterable[Job],
        *,
        pruned_at: datetime | None = None,
    ) -> int:
        """Persist minimal stale identity using an existing transaction."""
        values = list(jobs)
        if not values:
            return 0
        current = self._aware(pruned_at or datetime.now(UTC)).isoformat()
        connection.executemany(
            "UPDATE jobs SET canonical_url=?,last_seen_at=?,last_verified_at=? "
            "WHERE source=? AND source_board_id=? AND source_job_id=?",
            [
                (
                    str(job.canonical_url),
                    job.last_seen_at.isoformat(),
                    current,
                    job.source,
                    job.source_board_id,
                    job.source_job_id,
                )
                for job in values
            ],
        )
        connection.executemany(
            "INSERT OR REPLACE INTO job_retention_evidence (job_id,posted_at) "
            "SELECT id,? FROM jobs "
            "WHERE source=? AND source_board_id=? AND source_job_id=?",
            [
                (
                    self._aware(job.posted_at).isoformat(),
                    job.source,
                    job.source_board_id,
                    job.source_job_id,
                )
                for job in values
                if job.posted_at is not None
            ],
        )
        connection.executemany(
            "INSERT INTO job_identity_ledger "
            "(source,source_board_id,source_job_id,canonical_url,employer_id,company,"
            "first_seen_at,last_seen_at,pruned_at,was_delivered) "
            "VALUES (?,?,?,?,?,?,"
            "COALESCE((SELECT first_seen_at FROM jobs "
            "WHERE source=? AND source_board_id=? AND source_job_id=?),?),?,?,?) "
            "ON CONFLICT(source,source_board_id,source_job_id) DO UPDATE SET "
            "canonical_url=excluded.canonical_url,"
            "employer_id=COALESCE(excluded.employer_id,job_identity_ledger.employer_id),"
            "company=excluded.company,"
            "last_seen_at=excluded.last_seen_at,"
            "pruned_at=excluded.pruned_at,"
            "was_delivered=MAX(job_identity_ledger.was_delivered,excluded.was_delivered)",
            [
                (
                    job.source,
                    job.source_board_id,
                    job.source_job_id,
                    str(job.canonical_url),
                    job.employer_id,
                    job.company,
                    job.source,
                    job.source_board_id,
                    job.source_job_id,
                    job.discovered_at.isoformat(),
                    job.last_seen_at.isoformat(),
                    current,
                    0,
                )
                for job in values
            ],
        )
        return len(values)


    def _compact_invalidated_identities_in_connection(
        self,
        connection: sqlite3.Connection,
        identities: Iterable[tuple[str, str, str]],
        *,
        invalidated_at: datetime,
    ) -> int:
        """Close retained payloads without keeping full descriptions past invalidation."""
        current = self._aware(invalidated_at).isoformat()
        count = 0
        for source, board_id, source_job_id in dict.fromkeys(identities):
            row = connection.execute(
                "SELECT id,canonical_url,first_seen_at,last_seen_at,payload_json "
                "FROM jobs WHERE source=? AND source_board_id=? AND source_job_id=?",
                (source, board_id, source_job_id),
            ).fetchone()
            if row is None:
                continue

            job = Job.model_validate_json(row["payload_json"])
            if (
                job.source != source
                or job.source_board_id != board_id
                or job.source_job_id != source_job_id
            ):
                raise ValueError("invalidated job payload provenance is inconsistent")

            delivered = connection.execute(
                "SELECT 1 FROM group_deliveries WHERE job_id=? LIMIT 1",
                (job.id,),
            ).fetchone()
            exported = connection.execute(
                "SELECT 1 FROM exports WHERE job_id=? LIMIT 1",
                (job.id,),
            ).fetchone()
            connection.execute(
                "INSERT INTO job_identity_ledger "
                "(source,source_board_id,source_job_id,canonical_url,employer_id,company,"
                "first_seen_at,last_seen_at,pruned_at,was_delivered) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(source,source_board_id,source_job_id) DO UPDATE SET "
                "canonical_url=excluded.canonical_url,"
                "employer_id=COALESCE(job_identity_ledger.employer_id,excluded.employer_id),"
                "company=excluded.company,"
                "last_seen_at=excluded.last_seen_at,"
                "pruned_at=excluded.pruned_at,"
                "was_delivered=MAX(job_identity_ledger.was_delivered,excluded.was_delivered)",
                (
                    source,
                    board_id,
                    source_job_id,
                    row["canonical_url"],
                    job.employer_id,
                    job.company,
                    row["first_seen_at"],
                    row["last_seen_at"],
                    current,
                    1 if delivered is not None or exported is not None else 0,
                ),
            )
            connection.execute("DELETE FROM job_matches WHERE job_id=?", (job.id,))
            connection.execute(
                "DELETE FROM scoped_job_matches WHERE job_id=?", (job.id,)
            )
            compact = job.model_copy(
                update={
                    "description_text": None,
                    "description_html": None,
                    "raw_metadata": {},
                    "offices": [],
                }
            )
            connection.execute(
                "UPDATE jobs SET payload_json=?,lifecycle=?,last_verified_at=? "
                "WHERE id=?",
                (
                    compact.model_dump_json(),
                    JobLifecycle.CLOSED.value,
                    current,
                    job.id,
                ),
            )
            connection.execute(
                "DELETE FROM job_retention_evidence WHERE job_id=?",
                (job.id,),
            )
            count += 1
        return count

    def record_pruned_identities(
        self,
        jobs: Iterable[Job],
        *,
        pruned_at: datetime | None = None,
    ) -> int:
        """Persist minimal identity without storing a full stale job payload."""
        values = list(jobs)
        if not values:
            return 0
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._record_pruned_identities_in_connection(
                connection,
                values,
                pruned_at=pruned_at,
            )

    def prune_stale_inventory(
        self,
        *,
        retention_hours: int = 72,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """Purge stale active payloads while retaining minimal delivery identity."""
        if retention_hours < 1:
            raise ValueError("retention_hours must be at least 1")
        current = self._aware(now or datetime.now(UTC))
        cutoff = current.timestamp() - retention_hours * 3600
        counts = {
            "deleted_jobs": 0,
            "compacted_jobs": 0,
            "deleted_delivered_candidates": 0,
            "skipped_unresolved": 0,
        }
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            has_batches = (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_batches'"
                ).fetchone()
                is not None
            )
            if has_batches:
                cutoff_iso = datetime.fromtimestamp(cutoff, tz=UTC).isoformat()
                counts["deleted_delivered_candidates"] = connection.execute(
                    "DELETE FROM daily_batch_candidates WHERE batch_id IN ("
                    "SELECT batch_id FROM daily_batches "
                    "WHERE status='delivered' AND assembled_at<?"
                    ")",
                    (cutoff_iso,),
                ).rowcount

            rows = connection.execute(
                "SELECT j.id,j.canonical_url,j.first_seen_at,j.last_seen_at,j.payload_json,"
                "e.posted_at AS retention_posted_at "
                "FROM jobs j LEFT JOIN job_retention_evidence e ON e.job_id=j.id "
                "WHERE j.lifecycle!='closed' "
                "ORDER BY j.id"
            ).fetchall()
            for row in rows:
                job = Job.model_validate_json(row["payload_json"])
                first_seen = datetime.fromisoformat(row["first_seen_at"])
                retention_posted_at = (
                    datetime.fromisoformat(row["retention_posted_at"])
                    if row["retention_posted_at"]
                    else None
                )
                basis = retention_basis(
                    retention_posted_at=retention_posted_at,
                    posted_at=job.posted_at,
                    first_seen_at=first_seen,
                )
                if basis.timestamp() >= cutoff:
                    continue

                if has_batches:
                    unresolved = connection.execute(
                        "SELECT 1 FROM daily_batches b "
                        "LEFT JOIN daily_batch_candidates c ON c.batch_id=b.batch_id "
                        "LEFT JOIN daily_batch_items i ON i.batch_id=b.batch_id "
                        "WHERE b.status!='delivered' AND (c.job_id=? OR i.representative_job_id=?) "
                        "LIMIT 1",
                        (job.id, job.id),
                    ).fetchone()
                    if unresolved is not None:
                        counts["skipped_unresolved"] += 1
                        continue
                    selected_batch = connection.execute(
                        "SELECT 1 FROM daily_batch_items i JOIN daily_batches b "
                        "ON b.batch_id=i.batch_id "
                        "WHERE i.representative_job_id=? LIMIT 1",
                        (job.id,),
                    ).fetchone()
                    candidate_batch = connection.execute(
                        "SELECT 1 FROM daily_batch_candidates c JOIN daily_batches b "
                        "ON b.batch_id=c.batch_id "
                        "WHERE c.job_id=? LIMIT 1",
                        (job.id,),
                    ).fetchone()
                else:
                    selected_batch = None
                    candidate_batch = None

                delivered = connection.execute(
                    "SELECT 1 FROM group_deliveries WHERE job_id=? LIMIT 1", (job.id,)
                ).fetchone()
                exported = connection.execute(
                    "SELECT 1 FROM exports WHERE job_id=? LIMIT 1", (job.id,)
                ).fetchone()
                keep_identity_row = any(
                    value is not None
                    for value in (selected_batch, candidate_batch, delivered, exported)
                )

                connection.execute(
                    "INSERT INTO job_identity_ledger "
                    "(source,source_board_id,source_job_id,canonical_url,employer_id,company,"
                    "first_seen_at,last_seen_at,pruned_at,was_delivered) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(source,source_board_id,source_job_id) DO UPDATE SET "
                    "canonical_url=excluded.canonical_url,"
                    "employer_id=COALESCE(job_identity_ledger.employer_id,excluded.employer_id),"
                    "last_seen_at=excluded.last_seen_at,"
                    "pruned_at=excluded.pruned_at,"
                    "was_delivered=MAX(job_identity_ledger.was_delivered,excluded.was_delivered)",
                    (
                        job.source,
                        job.source_board_id,
                        job.source_job_id,
                        row["canonical_url"],
                        job.employer_id,
                        job.company,
                        row["first_seen_at"],
                        row["last_seen_at"],
                        current.isoformat(),
                        1 if delivered is not None or exported is not None else 0,
                    ),
                )
                connection.execute("DELETE FROM job_matches WHERE job_id=?", (job.id,))
                connection.execute(
                    "DELETE FROM scoped_job_matches WHERE job_id=?", (job.id,)
                )

                if keep_identity_row:
                    compact = job.model_copy(
                        update={
                            "description_text": None,
                            "description_html": None,
                            "raw_metadata": {},
                            "offices": [],
                        }
                    )
                    connection.execute(
                        "UPDATE jobs SET payload_json=?, lifecycle=? WHERE id=?",
                        (compact.model_dump_json(), JobLifecycle.CLOSED.value, job.id),
                    )
                    connection.execute(
                        "DELETE FROM job_retention_evidence WHERE job_id=?", (job.id,)
                    )
                    counts["compacted_jobs"] += 1
                    continue

                group = connection.execute(
                    "SELECT group_id FROM posting_delivery_groups WHERE job_id=?", (job.id,)
                ).fetchone()
                connection.execute("DELETE FROM delivery_keys WHERE job_id=?", (job.id,))
                connection.execute("DELETE FROM posting_delivery_groups WHERE job_id=?", (job.id,))
                connection.execute("DELETE FROM jobs WHERE id=?", (job.id,))
                counts["deleted_jobs"] += 1
                if group is not None:
                    connection.execute(
                        "DELETE FROM delivery_groups WHERE id=? "
                        "AND NOT EXISTS (SELECT 1 FROM posting_delivery_groups WHERE group_id=?) "
                        "AND NOT EXISTS (SELECT 1 FROM group_deliveries WHERE group_id=?)",
                        (group[0], group[0], group[0]),
                    )
        return counts

    def _assign_group(self, connection: sqlite3.Connection, job: Job) -> None:
        keys = delivery_keys(job)
        existing = connection.execute(
            "SELECT group_id FROM posting_delivery_groups WHERE job_id=?", (job.id,)
        ).fetchone()
        groups = {existing[0]} if existing else set()
        for kind, value in sorted(keys):
            groups.update(
                row[0]
                for row in connection.execute(
                    "SELECT g.group_id FROM delivery_keys k JOIN posting_delivery_groups g "
                    "ON g.job_id=k.job_id WHERE k.kind=? AND k.value=?",
                    (kind, value),
                )
            )
        own_group = str(
            uuid5(NAMESPACE_URL, json.dumps([job.source, job.source_board_id, job.source_job_id]))
        )
        group_id = min(groups | {own_group})
        connection.execute("INSERT OR IGNORE INTO delivery_groups VALUES (?)", (group_id,))
        # A group is historical delivery identity. Edits update evidence keys but
        # never reset delivery history. Merge history before deleting old groups.
        for old in sorted(groups - {group_id}):
            connection.execute(
                "INSERT OR IGNORE INTO group_deliveries "
                "SELECT ?, client_id, destination, job_id, exported_at FROM group_deliveries "
                "WHERE group_id=? ORDER BY exported_at, job_id",
                (group_id, old),
            )
            connection.execute("DELETE FROM group_deliveries WHERE group_id=?", (old,))
            connection.execute(
                "UPDATE posting_delivery_groups SET group_id=? WHERE group_id=?", (group_id, old)
            )
            connection.execute("DELETE FROM delivery_groups WHERE id=?", (old,))
        connection.execute(
            "INSERT OR REPLACE INTO posting_delivery_groups VALUES (?, ?)", (job.id, group_id)
        )
        connection.execute("DELETE FROM delivery_keys WHERE job_id=?", (job.id,))
        connection.executemany(
            "INSERT INTO delivery_keys VALUES (?, ?, ?)",
            [(job.id, kind, value) for kind, value in sorted(keys)],
        )

    def delivery_group_id(self, job_id: str) -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT group_id FROM posting_delivery_groups WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"posting has no delivery group: {job_id}")
            return row[0]

    def select_deliveries(self, jobs: list[Job], client_id: str, destination: str) -> list[Job]:
        representatives: dict[str, Job] = {}
        with self.connect() as connection:
            for job in sorted(jobs, key=representative_key):
                group = connection.execute(
                    "SELECT group_id FROM posting_delivery_groups WHERE job_id=?", (job.id,)
                ).fetchone()[0]
                exported = connection.execute(
                    "SELECT 1 FROM group_deliveries WHERE group_id=? AND client_id=? AND destination=?",
                    (group, client_id, destination),
                ).fetchone()
                if (
                    group not in representatives
                    and exported is None
                    and not self._is_historically_surfaced(
                        connection, job, client_id, destination
                    )
                ):
                    representatives[group] = job
        return [representatives[group] for group in sorted(representatives)]

    def is_historically_surfaced(
        self,
        job: Job,
        client_id: str,
        destination: str | None = None,
    ) -> bool:
        with self.connect() as connection:
            return self._is_historically_surfaced(
                connection, job, client_id, destination
            )

    @staticmethod
    def _is_historically_surfaced(
        connection: sqlite3.Connection,
        job: Job,
        client_id: str,
        destination: str | None = None,
    ) -> bool:
        identity = connection.execute(
            "SELECT 1 FROM historical_job_links WHERE client_id=? AND source=? "
            "AND source_board_id=? AND source_job_id=? LIMIT 1",
            (client_id, job.source, job.source_board_id, job.source_job_id),
        ).fetchone()
        if identity is not None:
            return True
        historical_url = connection.execute(
            "SELECT 1 FROM historical_job_links "
            "WHERE client_id=? AND normalized_url=? LIMIT 1",
            (client_id, str(job.canonical_url)),
        ).fetchone()
        if historical_url is not None:
            return True
        if destination is None:
            return False
        observed_identity = connection.execute(
            "SELECT 1 FROM destination_observed_links WHERE client_id=? "
            "AND destination=? AND source=? AND source_board_id=? "
            "AND source_job_id=? LIMIT 1",
            (
                client_id,
                destination,
                job.source,
                job.source_board_id,
                job.source_job_id,
            ),
        ).fetchone()
        if observed_identity is not None:
            return True
        return (
            connection.execute(
                "SELECT 1 FROM destination_observed_links "
                "WHERE client_id=? AND destination=? AND normalized_url=? LIMIT 1",
                (client_id, destination, str(job.canonical_url)),
            ).fetchone()
            is not None
        )

    def import_historical_records(
        self,
        *,
        client_id: str,
        workbook_sha256: str,
        records,
        blacklist_evidence=(),
        importer_version: str = "historical-operator-state-v1",
    ) -> tuple[int, int]:
        from job_scout.history import HistoricalBlacklistEvidence, HistoricalRecord

        values = list(records)
        blacklists = list(blacklist_evidence)
        if not all(isinstance(value, HistoricalRecord) for value in values):
            raise TypeError("historical records are required")
        if not all(isinstance(value, HistoricalBlacklistEvidence) for value in blacklists):
            raise TypeError("historical blacklist evidence is required")
        import_id = str(uuid5(NAMESPACE_URL, f"{client_id}:{workbook_sha256}"))
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT 1 FROM historical_imports WHERE id=?", (import_id,)
            ).fetchone()
            connection.execute(
                "INSERT OR IGNORE INTO historical_imports VALUES (?,?,?,?,?,?)",
                (import_id, client_id, workbook_sha256, now, importer_version, len(values)),
            )
            if existing:
                return 0, len(values)
            connection.executemany(
                "INSERT INTO historical_job_links (import_id,client_id,original_url,normalized_url,source,source_board_id,source_job_id,title,company,operator_status,source_sheet,source_row,imported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        import_id,
                        client_id,
                        v.original_url,
                        v.normalized_url,
                        v.source,
                        v.source_board_id,
                        v.source_job_id,
                        v.title,
                        v.company,
                        v.operator_status,
                        v.source_sheet,
                        v.source_row,
                        now,
                    )
                    for v in values
                ],
            )
            connection.executemany(
                "INSERT INTO historical_blacklist_evidence (import_id,client_id,value,kind,source_sheet,source_row,imported_at) VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        import_id,
                        client_id,
                        value.value,
                        value.kind,
                        value.source_sheet,
                        value.source_row,
                        now,
                    )
                    for value in blacklists
                ],
            )
            return len(values), 0

    def observe_destination_links(
        self,
        *,
        client_id: str,
        destination: str,
        links: Iterable[str],
    ) -> tuple[int, int]:
        """Persist links already visible in a client destination as prior surfacing."""
        from job_scout.history import source_identity
        from job_scout.normalization.core import canonicalize_url

        unique: dict[str, tuple[str | None, str | None, str | None]] = {}
        for raw in links:
            value = str(raw).strip()
            if not value:
                continue
            normalized = canonicalize_url(value)
            if not normalized.startswith(("http://", "https://")):
                raise ValueError("destination Job Link contains an invalid URL")
            unique.setdefault(normalized, source_identity(value))

        if not unique:
            return 0, 0
        now = datetime.now(UTC).isoformat()
        inserted = 0
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for normalized, identity in sorted(unique.items()):
                source, board, job_id = identity
                inserted += connection.execute(
                    "INSERT OR IGNORE INTO destination_observed_links "
                    "(client_id,destination,normalized_url,source,source_board_id,"
                    "source_job_id,first_observed_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        client_id,
                        destination,
                        normalized,
                        source,
                        board,
                        job_id,
                        now,
                    ),
                ).rowcount
        return inserted, len(unique)

    @staticmethod
    def _match_row(match: JobMatch) -> tuple[object, ...]:
        return (
            match.job_id,
            match.client_id,
            match.decision.value,
            match.score,
            json.dumps(match.matched_reasons),
            json.dumps(match.rejection_reasons),
            match.evaluated_at.isoformat(),
            match.matcher_version,
        )

    @staticmethod
    def _scoped_match_row(match: JobMatch, match_scope_id: str) -> tuple[object, ...]:
        scope = match_scope_id.strip()
        if not scope:
            raise ValueError("match_scope_id must not be blank")
        return (
            match.job_id,
            match.client_id,
            scope,
            match.decision.value,
            match.score,
            json.dumps(match.matched_reasons),
            json.dumps(match.rejection_reasons),
            match.evaluated_at.isoformat(),
            match.matcher_version,
        )

    def save_scoped_match(self, match: JobMatch, match_scope_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO scoped_job_matches "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                self._scoped_match_row(match, match_scope_id),
            )

    def save_match(self, match: JobMatch) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO job_matches VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                self._match_row(match),
            )

    def save_matches(self, matches: Iterable[JobMatch]) -> int:
        """Persist one client evaluation set in a single transaction/push."""
        values = list(matches)
        if not values:
            return 0
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "INSERT OR REPLACE INTO job_matches VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [self._match_row(match) for match in values],
            )
        return len(values)

    def is_exported(self, job_id: str, client_id: str, destination: str) -> bool:
        with self.connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM group_deliveries d JOIN posting_delivery_groups g "
                    "ON g.group_id=d.group_id WHERE g.job_id=? AND d.client_id=? AND d.destination=?",
                    (job_id, client_id, destination),
                ).fetchone()
                is not None
            )

    def mark_exported(self, job_id: str, client_id: str, destination: str) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if (
                connection.execute(
                    "SELECT 1 FROM posting_delivery_groups WHERE job_id=?", (job_id,)
                ).fetchone()
                is None
            ):
                raise ValueError(f"posting has no delivery group: {job_id}")
            inserted = connection.execute(
                "INSERT OR IGNORE INTO group_deliveries "
                "SELECT group_id, ?, ?, job_id, ? FROM posting_delivery_groups WHERE job_id=?",
                (client_id, destination, datetime.now(UTC).isoformat(), job_id),
            ).rowcount
            if not inserted:
                return
            connection.execute(
                "INSERT OR IGNORE INTO exports VALUES (?, ?, ?, ?)",
                (job_id, client_id, destination, datetime.now(UTC).isoformat()),
            )
