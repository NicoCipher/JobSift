from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from job_scout.dedupe.resolver import delivery_keys, representative_key
from job_scout.domain.models import Job, JobLifecycle, JobMatch

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
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def upsert_job(self, job: Job) -> JobLifecycle:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, content_fingerprint FROM jobs WHERE source=? AND source_board_id=? AND source_job_id=?",
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
                self._assign_group(connection, job)
                return JobLifecycle.NEW
            lifecycle = (
                JobLifecycle.CHANGED
                if row["content_fingerprint"] != job.content_fingerprint
                else JobLifecycle.SEEN
            )
            job.id = row["id"]
            connection.execute(
                "UPDATE jobs SET canonical_url=?, content_fingerprint=?, last_seen_at=?, last_verified_at=?, lifecycle=?, payload_json=? WHERE id=?",
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
            self._assign_group(connection, job)
            return lifecycle

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
                    and not self._is_historically_surfaced(connection, job, client_id)
                ):
                    representatives[group] = job
        return [representatives[group] for group in sorted(representatives)]

    def is_historically_surfaced(self, job: Job, client_id: str) -> bool:
        with self.connect() as connection:
            return self._is_historically_surfaced(connection, job, client_id)

    @staticmethod
    def _is_historically_surfaced(connection: sqlite3.Connection, job: Job, client_id: str) -> bool:
        identity = connection.execute(
            "SELECT 1 FROM historical_job_links WHERE client_id=? AND source=? "
            "AND source_board_id=? AND source_job_id=? LIMIT 1",
            (client_id, job.source, job.source_board_id, job.source_job_id),
        ).fetchone()
        if identity is not None:
            return True
        return (
            connection.execute(
                "SELECT 1 FROM historical_job_links WHERE client_id=? AND normalized_url=? LIMIT 1",
                (client_id, str(job.canonical_url)),
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

    def save_match(self, match: JobMatch) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO job_matches VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    match.job_id,
                    match.client_id,
                    match.decision.value,
                    match.score,
                    json.dumps(match.matched_reasons),
                    json.dumps(match.rejection_reasons),
                    match.evaluated_at.isoformat(),
                    match.matcher_version,
                ),
            )

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
