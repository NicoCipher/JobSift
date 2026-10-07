"""Additive batch persistence over the existing posting/group/history repository."""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid4, uuid5

from job_scout.dedupe.resolver import DEDUPE_VERSION
from job_scout.domain.daily_batch import (
    BatchConflict,
    DailyBatchItem,
    DailyBatchRequest,
    DailyBatchResult,
    RetryableDestinationConflict,
)
from job_scout.domain.models import Job, JobMatch, MatchDecision
from job_scout.normalization.company import employer_key
from job_scout.posting_freshness import posting_freshness_disposition
from job_scout.storage.factory import postgres_database_url

if TYPE_CHECKING:
    from job_scout.storage.sqlite import SQLiteRepository

BATCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_batches (
  batch_id TEXT PRIMARY KEY, generation_id TEXT, client_id TEXT NOT NULL, destination TEXT NOT NULL,
  idempotency_key TEXT NOT NULL, requested_quota INTEGER NOT NULL CHECK(requested_quota > 0),
  selected_count INTEGER NOT NULL CHECK(selected_count >= 0 AND selected_count <= requested_quota),
  shortfall INTEGER NOT NULL CHECK(shortfall = requested_quota - selected_count),
  status TEXT NOT NULL CHECK(status IN ('prepared','failed','delivered')),
  assembled_at TEXT NOT NULL, delivered_at TEXT,
  request_json TEXT NOT NULL, counts_json TEXT NOT NULL, dedupe_version TEXT NOT NULL,
  export_before_sha256 TEXT, export_after_sha256 TEXT, error TEXT,
  UNIQUE(client_id, destination, idempotency_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_batch_destination
  ON daily_batches(destination) WHERE export_after_sha256 IS NOT NULL AND status != 'delivered';
CREATE TABLE IF NOT EXISTS daily_batch_items (
  batch_id TEXT NOT NULL REFERENCES daily_batches(batch_id), ordinal INTEGER NOT NULL CHECK(ordinal>0),
  delivery_group_id TEXT NOT NULL,
  representative_job_id TEXT NOT NULL REFERENCES jobs(id),
  evidence_sha256 TEXT NOT NULL, matcher_version TEXT NOT NULL, export_row_json TEXT NOT NULL,
  PRIMARY KEY(batch_id, ordinal), UNIQUE(batch_id, delivery_group_id)
);
CREATE TABLE IF NOT EXISTS daily_batch_candidates (
  batch_id TEXT NOT NULL REFERENCES daily_batches(batch_id), job_id TEXT NOT NULL REFERENCES jobs(id),
  delivery_group_id TEXT NOT NULL, evidence_sha256 TEXT NOT NULL, decision TEXT NOT NULL,
  disposition TEXT NOT NULL, PRIMARY KEY(batch_id, job_id)
);
"""


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _chunks(values, size: int = 500):
    for start in range(0, len(values), size):
        yield values[start : start + size]


@dataclass(frozen=True)
class BatchCandidate:
    job: Job
    match: JobMatch
    group_id: str
    evidence_sha256: str
    employer_key: str
    historical: bool
    delivered: bool
    employer_recently_delivered: bool = False


def posting_evidence(job: Job, match: JobMatch) -> str:
    payload = job.model_dump(mode="json")
    payload["eligible_countries"] = sorted(payload["eligible_countries"])
    # Collection bookkeeping is not job content. Hourly refreshes may change
    # these timestamps while a reviewed batch is waiting without changing the
    # posting itself.
    payload.pop("discovered_at", None)
    payload.pop("last_seen_at", None)
    match_payload = match.model_dump(mode="json")
    # Re-evaluating the same semantic match at a later time must not invalidate
    # an already prepared batch for another destination. The batch request
    # separately freezes its evaluation scope/time; delivery evidence should
    # change only when the posting or match result changes.
    match_payload.pop("evaluated_at", None)
    return digest([payload, match_payload])


class DailyBatchStore:
    def __init__(self, repository: SQLiteRepository):
        self.repository = repository
        with repository.connect() as connection:
            connection.executescript(BATCH_SCHEMA)
            # Serialize the legacy-column check, ALTER, and backfill. A second
            # initializer waits here, then re-reads the post-migration schema
            # instead of racing the same ALTER TABLE.
            connection.execute("BEGIN IMMEDIATE")
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(daily_batches)")
            }
            if "generation_id" not in columns:
                connection.execute("ALTER TABLE daily_batches ADD COLUMN generation_id TEXT")
            for row in connection.execute(
                "SELECT batch_id FROM daily_batches WHERE generation_id IS NULL"
            ).fetchall():
                connection.execute(
                    "UPDATE daily_batches SET generation_id=? WHERE batch_id=?",
                    (str(uuid4()), row["batch_id"]),
                )

    @staticmethod
    def _groups_for_jobs(c, job_ids: tuple[str, ...] | list[str]) -> dict[str, str]:
        groups: dict[str, str] = {}
        values = sorted(set(job_ids))
        for chunk in _chunks(values):
            placeholders = ",".join("?" for _ in chunk)
            rows = c.execute(
                "SELECT job_id,group_id FROM posting_delivery_groups "
                f"WHERE job_id IN ({placeholders})",
                chunk,
            ).fetchall()
            groups.update({row["job_id"]: row["group_id"] for row in rows})
        return groups

    @staticmethod
    def _delivered_groups(c, group_ids, client_id: str, destination: str) -> set[str]:
        delivered: set[str] = set()
        values = sorted(set(group_ids))
        for chunk in _chunks(values):
            placeholders = ",".join("?" for _ in chunk)
            rows = c.execute(
                "SELECT DISTINCT group_id FROM group_deliveries "
                f"WHERE group_id IN ({placeholders}) "
                "AND client_id=? AND destination=?",
                [*chunk, client_id, destination],
            ).fetchall()
            delivered.update(row["group_id"] for row in rows)
        return delivered

    @staticmethod
    def _historical_groups(
        c,
        group_ids,
        client_id: str,
        destination: str | None,
    ) -> set[str]:
        """Resolve group-level prior surfacing with one bounded read per chunk."""
        historical: set[str] = set()
        values = sorted(set(group_ids))
        for chunk in _chunks(values):
            placeholders = ",".join("?" for _ in chunk)
            destination_clause = ""
            parameters = [*chunk, client_id]
            if destination is not None:
                destination_clause = (
                    " OR EXISTS ("
                    "SELECT 1 FROM destination_observed_links d "
                    "WHERE d.client_id=? AND d.destination=? AND ("
                    "(d.source=j.source AND d.source_board_id=j.source_board_id "
                    "AND d.source_job_id=j.source_job_id) "
                    "OR d.normalized_url=j.canonical_url))"
                )
                parameters.extend([client_id, destination])
            rows = c.execute(
                "SELECT DISTINCT g.group_id "
                "FROM posting_delivery_groups g JOIN jobs j ON j.id=g.job_id "
                f"WHERE g.group_id IN ({placeholders}) AND ("
                "EXISTS (SELECT 1 FROM historical_job_links h "
                "WHERE h.client_id=? AND ("
                "(h.source=j.source AND h.source_board_id=j.source_board_id "
                "AND h.source_job_id=j.source_job_id) "
                "OR h.normalized_url=j.canonical_url))"
                f"{destination_clause})",
                parameters,
            ).fetchall()
            historical.update(row["group_id"] for row in rows)
        return historical

    def _candidates(
        self,
        c,
        client_id,
        job_ids,
        destination=None,
        match_scope_id: str | None = None,
    ):
        """Load candidate evidence in bounded batches instead of N+1 reads."""
        ordered_ids = sorted(job_ids)
        rows_by_job = {}
        for chunk in _chunks(ordered_ids):
            placeholders = ",".join("?" for _ in chunk)
            if match_scope_id is None:
                rows = c.execute(
                    "SELECT j.id AS job_id,j.payload_json,g.group_id,"
                    "m.decision,m.score,m.matched_reasons_json,"
                    "m.rejection_reasons_json,m.evaluated_at,m.matcher_version "
                    "FROM jobs j JOIN posting_delivery_groups g ON g.job_id=j.id "
                    "JOIN job_matches m ON m.job_id=j.id "
                    f"WHERE j.id IN ({placeholders}) AND m.client_id=?",
                    [*chunk, client_id],
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT j.id AS job_id,j.payload_json,g.group_id,"
                    "m.decision,m.score,m.matched_reasons_json,"
                    "m.rejection_reasons_json,m.evaluated_at,m.matcher_version "
                    "FROM jobs j JOIN posting_delivery_groups g ON g.job_id=j.id "
                    "JOIN scoped_job_matches m ON m.job_id=j.id "
                    f"WHERE j.id IN ({placeholders}) "
                    "AND m.client_id=? AND m.match_scope_id=?",
                    [*chunk, client_id, match_scope_id],
                ).fetchall()
            rows_by_job.update({row["job_id"]: row for row in rows})

        missing = [job_id for job_id in ordered_ids if job_id not in rows_by_job]
        if missing:
            raise BatchConflict(
                f"missing authoritative posting/match: {missing[0]}"
            )

        group_ids = [rows_by_job[job_id]["group_id"] for job_id in ordered_ids]
        historical_groups = self._historical_groups(
            c,
            group_ids,
            client_id,
            destination,
        )
        candidates = []
        for job_id in ordered_ids:
            row = rows_by_job[job_id]
            job = Job.model_validate_json(row["payload_json"])
            match = JobMatch(
                job_id=job_id,
                client_id=client_id,
                decision=row["decision"],
                score=row["score"],
                matched_reasons=json.loads(row["matched_reasons_json"]),
                rejection_reasons=json.loads(row["rejection_reasons_json"]),
                evaluated_at=row["evaluated_at"],
                matcher_version=row["matcher_version"],
            )
            group = row["group_id"]
            candidates.append(
                BatchCandidate(
                    job=job,
                    match=match,
                    group_id=group,
                    evidence_sha256=posting_evidence(job, match),
                    employer_key=employer_key(job),
                    historical=group in historical_groups,
                    delivered=False,
                )
            )
        return candidates

    def evidence_digest(
        self,
        client_id: str,
        job_ids: tuple[str, ...],
        *,
        match_scope_id: str | None = None,
    ) -> str:
        """Capture current authoritative rows for an explicit match scope when provided."""
        if len(set(job_ids)) != len(job_ids):
            raise BatchConflict("candidate IDs must be unique")
        with self.repository.connect() as c:
            c.execute("BEGIN")
            return self._digest(
                self._candidates(
                    c,
                    client_id,
                    job_ids,
                    match_scope_id=match_scope_id,
                )
            )

    @staticmethod
    def _digest(candidates):
        return digest([(v.job.id, v.evidence_sha256) for v in candidates])

    @staticmethod
    def _load(c, batch_id):
        row = c.execute("SELECT * FROM daily_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise BatchConflict("batch not found")
        items = c.execute(
            "SELECT * FROM daily_batch_items WHERE batch_id=? ORDER BY ordinal", (batch_id,)
        ).fetchall()
        try:
            generation_id = row["generation_id"]
        except (IndexError, KeyError):
            # Read-only inspection may encounter a pre-migration database.
            # Normal store construction migrates/backfills before loading.
            generation_id = None
        return DailyBatchResult(
            batch_id=batch_id,
            generation_id=generation_id,
            request=json.loads(row["request_json"]),
            status=row["status"],
            assembled_at=row["assembled_at"],
            delivered_at=row["delivered_at"],
            counts=json.loads(row["counts_json"]),
            selected_count=row["selected_count"],
            shortfall=row["shortfall"],
            dedupe_version=row["dedupe_version"],
            error=row["error"],
            items=tuple(
                DailyBatchItem(**{k: item[k] for k in DailyBatchItem.model_fields})
                for item in items
            ),
        )

    def get(self, batch_id):
        with self.repository.connect() as c:
            return self._load(c, batch_id)

    def export_journal(
        self, batch_id: str, *, expected_generation_id: str | None = None
    ) -> tuple[str | None, str | None]:
        """Return frozen destination digests for one exact batch generation."""
        with self.repository.connect() as c:
            row = c.execute(
                "SELECT generation_id,export_before_sha256,export_after_sha256 "
                "FROM daily_batches WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
        if row is None:
            raise BatchConflict("batch not found")
        if (
            expected_generation_id is not None
            and row["generation_id"] != expected_generation_id
        ):
            raise BatchConflict("batch revision changed")
        return row["export_before_sha256"], row["export_after_sha256"]

    def remove_prepared_items(
        self,
        batch_id: str,
        ordinals: tuple[int, ...],
        *,
        expected_generation_id: str | None,
    ) -> DailyBatchResult:
        """Remove operator-rejected rows from an unpublished prepared batch.

        The mutation is generation-bound so a review decision cannot silently
        apply to a newer batch revision. Candidate evidence remains immutable;
        removed representatives are retained in the candidate journal with an
        explicit operator_removed disposition.
        """
        if expected_generation_id is None:
            raise BatchConflict("batch generation missing")
        requested = tuple(sorted(set(ordinals)))
        if not requested:
            return self.get(batch_id)
        if any(value < 1 for value in requested):
            raise BatchConflict("review item ordinals must be positive")

        with self.repository.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            result = self._load(c, batch_id)
            if result.generation_id != expected_generation_id:
                raise BatchConflict("batch revision changed")
            journal = c.execute(
                "SELECT status,delivered_at,export_before_sha256,export_after_sha256 "
                "FROM daily_batches WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            if (
                journal is None
                or journal["status"] != "prepared"
                or journal["delivered_at"] is not None
                or journal["export_before_sha256"] is not None
                or journal["export_after_sha256"] is not None
            ):
                raise BatchConflict("only an unpublished prepared batch can be edited")

            rows = c.execute(
                "SELECT * FROM daily_batch_items WHERE batch_id=? ORDER BY ordinal",
                (batch_id,),
            ).fetchall()
            known = {int(row["ordinal"]) for row in rows}
            unknown = set(requested) - known
            if unknown:
                raise BatchConflict("review selection contains an unknown batch item")
            if len(requested) >= len(rows):
                raise BatchConflict("cannot remove every job; discard the batch instead")

            removed = [row for row in rows if int(row["ordinal"]) in requested]
            kept = [row for row in rows if int(row["ordinal"]) not in requested]
            removed_job_ids = [row["representative_job_id"] for row in removed]

            if removed_job_ids:
                placeholders = ",".join("?" for _ in removed_job_ids)
                c.execute(
                    "UPDATE daily_batch_candidates SET disposition='operator_removed' "
                    f"WHERE batch_id=? AND job_id IN ({placeholders})",
                    [batch_id, *removed_job_ids],
                )

            c.execute("DELETE FROM daily_batch_items WHERE batch_id=?", (batch_id,))
            c.executemany(
                "INSERT INTO daily_batch_items VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        batch_id,
                        ordinal,
                        row["delivery_group_id"],
                        row["representative_job_id"],
                        row["evidence_sha256"],
                        row["matcher_version"],
                        row["export_row_json"],
                    )
                    for ordinal, row in enumerate(kept, 1)
                ],
            )
            counts = result.counts.model_copy(update={"selected_groups": len(kept)})
            new_generation = str(uuid4())
            c.execute(
                "UPDATE daily_batches SET generation_id=?,selected_count=?,shortfall=?,"
                "counts_json=? WHERE batch_id=? AND generation_id=?",
                (
                    new_generation,
                    len(kept),
                    result.request.requested_quota - len(kept),
                    counts.model_dump_json(),
                    batch_id,
                    expected_generation_id,
                ),
            )
            if c.rowcount != 1:
                raise BatchConflict("batch revision changed")
            return self._load(c, batch_id)

    def discard_prepared(
        self, batch_id: str, *, expected_generation_id: str | None
    ) -> DailyBatchResult:
        """Delete only the exact unpublished generation the caller already loaded."""
        if expected_generation_id is None:
            raise BatchConflict("batch generation missing")
        with self.repository.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT generation_id,status,delivered_at,export_after_sha256 "
                "FROM daily_batches WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            if row is None:
                raise BatchConflict("batch not found")
            if row["generation_id"] != expected_generation_id:
                raise BatchConflict("batch revision changed")
            if (
                row["status"] not in {"prepared", "failed"}
                or row["delivered_at"] is not None
                or row["export_after_sha256"] is not None
            ):
                raise BatchConflict("only an unpublished batch can be discarded")
            result = self._load(c, batch_id)
            c.execute(
                "DELETE FROM daily_batch_candidates WHERE batch_id=? AND EXISTS "
                "(SELECT 1 FROM daily_batches WHERE batch_id=? AND generation_id=?)",
                (batch_id, batch_id, expected_generation_id),
            )
            c.execute(
                "DELETE FROM daily_batch_items WHERE batch_id=? AND EXISTS "
                "(SELECT 1 FROM daily_batches WHERE batch_id=? AND generation_id=?)",
                (batch_id, batch_id, expected_generation_id),
            )
            deleted = c.execute(
                "DELETE FROM daily_batches WHERE batch_id=? AND generation_id=?",
                (batch_id, expected_generation_id),
            ).rowcount
            if deleted != 1:
                raise BatchConflict("batch revision changed")
            return result

    def recover_unresolved(self, client_id: str, destination: str) -> DailyBatchResult | None:
        """Resolve safe stale failures and return the next batch requiring action.

        State machine, evaluated under one write lock:
        - stale final-freshness failure + no journal/delivery -> delete and continue;
        - prepared -> return for release;
        - any journaled non-delivered batch -> return for reconciliation;
        - every other failed/no-journal batch -> leave durable and ignore here.
        """
        with self.repository.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            while True:
                row = c.execute(
                    "SELECT batch_id,status,error,delivered_at,export_before_sha256,"
                    "export_after_sha256 FROM daily_batches "
                    "WHERE client_id=? AND destination=? AND status!='delivered' "
                    "AND (status='prepared' OR export_before_sha256 IS NOT NULL "
                    "OR export_after_sha256 IS NOT NULL "
                    "OR (status='failed' AND delivered_at IS NULL "
                    "AND error LIKE 'prepared posting is no longer fresh at delivery:%')) "
                    "ORDER BY assembled_at,batch_id LIMIT 1",
                    (client_id, destination),
                ).fetchone()
                if row is None:
                    return None
                stale_unpublished = (
                    row["status"] == "failed"
                    and (row["error"] or "").startswith(
                        "prepared posting is no longer fresh at delivery:"
                    )
                    and row["delivered_at"] is None
                    and row["export_before_sha256"] is None
                    and row["export_after_sha256"] is None
                )
                if not stale_unpublished:
                    return self._load(c, row["batch_id"])
                batch_id = row["batch_id"]
                c.execute("DELETE FROM daily_batch_candidates WHERE batch_id=?", (batch_id,))
                c.execute("DELETE FROM daily_batch_items WHERE batch_id=?", (batch_id,))
                c.execute("DELETE FROM daily_batches WHERE batch_id=?", (batch_id,))

    def export_rows(self, batch_id: str) -> list[dict[str, str]]:
        """Return the frozen rows for operator review, never mutable current postings."""
        with self.repository.connect() as c:
            result = self._load(c, batch_id)
            return self._frozen_rows(c, result)

    @staticmethod
    def _frozen_rows(c, result: DailyBatchResult) -> list[dict[str, str]]:
        rows = [
            json.loads(row[0])
            for row in c.execute(
                "SELECT export_row_json FROM daily_batch_items WHERE batch_id=? ORDER BY ordinal",
                (result.batch_id,),
            )
        ]
        if len(rows) != result.selected_count:
            raise BatchConflict("batch items do not match the selected count")
        return rows

    @staticmethod
    def review_readonly(
        database: str, batch_id: str
    ) -> tuple[DailyBatchResult, list[dict[str, str]]]:
        """Read a prepared snapshot from the explicitly selected backend without mutation."""
        database_url = postgres_database_url()
        if database_url is not None:
            try:
                import psycopg
                from psycopg.rows import dict_row

                from job_scout.storage.postgres import PostgresConnection
            except ImportError as error:
                raise RuntimeError(
                    "install job-scout[postgres] for Neon/Postgres persistence"
                ) from error

            raw = None
            try:
                raw = psycopg.connect(
                    database_url,
                    row_factory=dict_row,
                    autocommit=False,
                    connect_timeout=5,
                )
                connection = PostgresConnection(raw)
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute("SET LOCAL statement_timeout = '5s'")
                result = DailyBatchStore._load(connection, batch_id)
                rows = DailyBatchStore._frozen_rows(connection, result)
                raw.rollback()
                return result, rows
            except psycopg.Error as error:
                if raw is not None:
                    raw.rollback()
                raise sqlite3.DatabaseError(str(error)) from error
            finally:
                if raw is not None:
                    raw.close()

        uri = Path(database).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            result = DailyBatchStore._load(connection, batch_id)
            return result, DailyBatchStore._frozen_rows(connection, result)

    def prepare(self, request, assemble):
        with self.repository.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            old = c.execute(
                "SELECT batch_id,request_json FROM daily_batches "
                "WHERE client_id=? AND destination=? AND idempotency_key=?",
                (request.client_id, request.destination, request.idempotency_key),
            ).fetchone()
            request_json = canonical(request.model_dump(mode="json"))
            if old:
                stored_request_json = canonical(
                    DailyBatchRequest.model_validate_json(
                        old["request_json"]
                    ).model_dump(mode="json")
                )
                if stored_request_json != request_json:
                    raise BatchConflict("idempotency key reused with incompatible request")
                return self._load(c, old["batch_id"])
            candidates = self._candidates(
                c,
                request.client_id,
                request.candidate_job_ids,
                request.destination,
                request.match_scope_id,
            )
            if self._digest(candidates) != request.evidence_sha256:
                raise BatchConflict("candidate evidence changed; capture an explicit new scope")
            recent_employers: set[str] = set()
            if request.employer_cooldown_days:
                cutoff = (datetime.now(UTC) - timedelta(days=request.employer_cooldown_days)).isoformat()
                delivered_jobs = c.execute(
                    "SELECT j.payload_json FROM group_deliveries d "
                    "JOIN jobs j ON j.id=d.job_id "
                    "WHERE d.client_id=? AND d.destination=? AND d.exported_at>=?",
                    (request.client_id, request.destination, cutoff),
                ).fetchall()
                recent_employers = {
                    employer_key(Job.model_validate_json(row[0])) for row in delivered_jobs
                }

            delivered_groups = self._delivered_groups(
                c,
                (v.group_id for v in candidates),
                request.client_id,
                request.destination,
            )
            scoped = [
                BatchCandidate(
                    job=v.job,
                    match=v.match,
                    group_id=v.group_id,
                    evidence_sha256=v.evidence_sha256,
                    employer_key=v.employer_key,
                    historical=v.historical,
                    delivered=v.group_id in delivered_groups,
                    employer_recently_delivered=v.employer_key in recent_employers,
                )
                for v in candidates
            ]
            counts, chosen, dispositions, rows = assemble(request, scoped)
            batch_id = str(
                uuid5(
                    NAMESPACE_URL,
                    canonical(
                        [
                            "daily-batch-v1",
                            request.client_id,
                            request.destination,
                            request.idempotency_key,
                        ]
                    ),
                )
            )
            c.execute(
                "INSERT INTO daily_batches (batch_id,generation_id,client_id,destination,idempotency_key,"
                "requested_quota,selected_count,shortfall,status,assembled_at,request_json,"
                "counts_json,dedupe_version) VALUES (?,?,?,?,?,?,?,?,'prepared',?,?,?,?)",
                (
                    batch_id,
                    str(uuid4()),
                    request.client_id,
                    request.destination,
                    request.idempotency_key,
                    request.requested_quota,
                    len(chosen),
                    request.requested_quota - len(chosen),
                    datetime.now(UTC).isoformat(),
                    request_json,
                    counts.model_dump_json(),
                    DEDUPE_VERSION,
                ),
            )
            c.executemany(
                "INSERT INTO daily_batch_items VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        batch_id,
                        n,
                        v.group_id,
                        v.job.id,
                        v.evidence_sha256,
                        v.match.matcher_version,
                        canonical(export),
                    )
                    for n, (v, export) in enumerate(zip(chosen, rows), 1)
                ],
            )
            c.executemany(
                "INSERT INTO daily_batch_candidates VALUES (?,?,?,?,?,?)",
                [
                    (
                        batch_id,
                        v.job.id,
                        v.group_id,
                        v.evidence_sha256,
                        v.match.decision.value,
                        dispositions[v.job.id],
                    )
                    for v in scoped
                ],
            )
            return self._load(c, batch_id)

    def _verify_selected(self, c, result):
        candidates = self._candidates(
            c,
            result.request.client_id,
            tuple(i.representative_job_id for i in result.items),
            result.request.destination,
            result.request.match_scope_id,
        )
        expected = {i.representative_job_id: i.evidence_sha256 for i in result.items}
        if len({v.group_id for v in candidates}) != len(candidates):
            raise BatchConflict("prepared groups merged; selection cannot be silently changed")
        delivered_groups = self._delivered_groups(
            c,
            (v.group_id for v in candidates),
            result.request.client_id,
            result.request.destination,
        )
        for v in candidates:
            freshness = posting_freshness_disposition(
                posted_at=v.job.posted_at,
                max_age_hours=result.request.max_posting_age_hours,
                unknown_policy=result.request.unknown_posting_age_policy,
                evaluated_at=datetime.now(UTC),
            )
            if freshness is not None:
                raise BatchConflict(
                    f"prepared posting is no longer fresh at delivery: {freshness}"
                )
            if (
                v.evidence_sha256 != expected[v.job.id]
                or v.historical
                or v.match.decision
                not in (
                    {MatchDecision.STRONG_MATCH, MatchDecision.POSSIBLE_MATCH}
                    | (
                        {MatchDecision.NEEDS_REVIEW}
                        if result.request.include_needs_review
                        else set()
                    )
                )
                or v.group_id in delivered_groups
            ):
                raise BatchConflict("prepared evidence or delivery state changed")

    @staticmethod
    def _require_generation(
        result: DailyBatchResult, expected_generation_id: str | None
    ) -> str:
        generation_id = expected_generation_id or result.generation_id
        if generation_id is None:
            raise BatchConflict("batch generation missing")
        if result.generation_id != generation_id:
            raise BatchConflict("batch revision changed")
        return generation_id

    def fail(
        self,
        batch_id,
        error,
        *,
        expected_generation_id: str | None = None,
    ):
        with self.repository.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            result = self._load(c, batch_id)
            generation_id = self._require_generation(result, expected_generation_id)
            c.execute(
                "UPDATE daily_batches SET status='failed',error=? WHERE batch_id=? "
                "AND generation_id=? AND status!='delivered'",
                (str(error), batch_id, generation_id),
            )
            return self._load(c, batch_id)

    def finalize(
        self,
        batch_id,
        plan,
        inspect,
        publish,
        *,
        expected_generation_id: str | None = None,
    ):
        """Journal and deliver only the exact generation that was guarded."""
        generation_id = expected_generation_id
        try:
            with self.repository.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                result = self._load(c, batch_id)
                generation_id = self._require_generation(result, generation_id)
                if result.status == "delivered":
                    return result
                pending = c.execute(
                    "SELECT batch_id FROM daily_batches WHERE destination=? AND status!='delivered' "
                    "AND export_after_sha256 IS NOT NULL AND batch_id!=?",
                    (result.request.destination, batch_id),
                ).fetchone()
                if pending:
                    raise BatchConflict("destination has an unresolved batch export")
                journal = c.execute(
                    "SELECT export_after_sha256 FROM daily_batches WHERE batch_id=?", (batch_id,)
                ).fetchone()[0]
                if journal is None:
                    self._verify_selected(c, result)
                    rows = [
                        json.loads(r[0])
                        for r in c.execute(
                            "SELECT export_row_json FROM daily_batch_items WHERE batch_id=? "
                            "ORDER BY ordinal",
                            (batch_id,),
                        )
                    ]
                    before, after = plan(rows)
                    c.execute(
                        "UPDATE daily_batches SET export_before_sha256=?, "
                        "export_after_sha256=?, error=NULL WHERE batch_id=?",
                        (before, after, batch_id),
                    )
            with self.repository.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                result = self._load(c, batch_id)
                self._require_generation(result, generation_id)
                if result.status == "delivered":
                    return result
                row = c.execute(
                    "SELECT * FROM daily_batches WHERE batch_id=?", (batch_id,)
                ).fetchone()
                before, after = row["export_before_sha256"], row["export_after_sha256"]
                current = inspect()
                if current != after:
                    if current != before:
                        raise BatchConflict(
                            "destination differs from both journal states; reconcile"
                        )
                    self._verify_selected(c, result)
                rows = [
                    json.loads(r[0])
                    for r in c.execute(
                        "SELECT export_row_json FROM daily_batch_items WHERE batch_id=? ORDER BY ordinal",
                        (batch_id,),
                    )
                ]
                publish(rows, before, after)
                now = datetime.now(UTC).isoformat()
                selected_ids = [
                    item.representative_job_id for item in result.items
                ]
                current_groups = self._groups_for_jobs(c, selected_ids)
                missing_groups = [
                    job_id for job_id in selected_ids if job_id not in current_groups
                ]
                if missing_groups:
                    raise BatchConflict(
                        f"posting has no delivery group: {missing_groups[0]}"
                    )
                c.executemany(
                    "INSERT OR IGNORE INTO group_deliveries VALUES (?,?,?,?,?)",
                    [
                        (
                            current_groups[item.representative_job_id],
                            result.request.client_id,
                            result.request.destination,
                            item.representative_job_id,
                            now,
                        )
                        for item in result.items
                    ],
                )
                c.executemany(
                    "INSERT OR IGNORE INTO exports VALUES (?,?,?,?)",
                    [
                        (
                            item.representative_job_id,
                            result.request.client_id,
                            result.request.destination,
                            now,
                        )
                        for item in result.items
                    ],
                )
                c.execute(
                    "UPDATE daily_batches SET status='delivered',delivered_at=?,error=NULL "
                    "WHERE batch_id=?",
                    (now, batch_id),
                )
                return self._load(c, batch_id)
        except (OSError, sqlite3.Error, ValueError, csv.Error) as error:
            if isinstance(error, RetryableDestinationConflict):
                raise
            if isinstance(error, BatchConflict) and str(error) in {
                "batch not found",
                "batch revision changed",
                "batch generation missing",
            }:
                raise
            return self.fail(
                batch_id, error, expected_generation_id=generation_id
            )
