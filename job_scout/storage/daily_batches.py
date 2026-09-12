"""Additive batch persistence over the existing posting/group/history repository."""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from job_scout.dedupe.resolver import DEDUPE_VERSION
from job_scout.domain.daily_batch import BatchConflict, DailyBatchItem, DailyBatchResult
from job_scout.domain.models import Job, JobMatch, MatchDecision

if TYPE_CHECKING:
    from job_scout.storage.sqlite import SQLiteRepository

BATCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_batches (
  batch_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, destination TEXT NOT NULL,
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


@dataclass(frozen=True)
class BatchCandidate:
    job: Job
    match: JobMatch
    group_id: str
    evidence_sha256: str
    historical: bool
    delivered: bool


def posting_evidence(job: Job, match: JobMatch) -> str:
    payload = job.model_dump(mode="json")
    payload["eligible_countries"] = sorted(payload["eligible_countries"])
    return digest([payload, match.model_dump(mode="json")])


class DailyBatchStore:
    def __init__(self, repository: SQLiteRepository):
        self.repository = repository
        with repository.connect() as connection:
            connection.executescript(BATCH_SCHEMA)

    def _candidates(self, c, client_id, job_ids):
        candidates = []
        group_states = {}
        for job_id in sorted(job_ids):
            row = c.execute(
                "SELECT j.payload_json,g.group_id,m.* FROM jobs j "
                "JOIN posting_delivery_groups g ON g.job_id=j.id "
                "JOIN job_matches m ON m.job_id=j.id WHERE j.id=? AND m.client_id=?",
                (job_id, client_id),
            ).fetchone()
            if row is None:
                raise BatchConflict(f"missing authoritative posting/match: {job_id}")
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
            if group not in group_states:
                # Historical surfacing of any persisted member suppresses the practical group.
                members = c.execute(
                    "SELECT j.payload_json FROM jobs j JOIN posting_delivery_groups g "
                    "ON g.job_id=j.id WHERE g.group_id=? ORDER BY j.id",
                    (group,),
                ).fetchall()
                group_states[group] = any(
                    self.repository._is_historically_surfaced(
                        c, Job.model_validate_json(member[0]), client_id
                    )
                    for member in members
                )
            candidates.append(
                BatchCandidate(
                    job, match, group, posting_evidence(job, match), group_states[group], False
                )
            )
        return candidates

    def evidence_digest(self, client_id: str, job_ids: tuple[str, ...]) -> str:
        """Capture current authoritative rows, without asserting a legacy brief association."""
        if len(set(job_ids)) != len(job_ids):
            raise BatchConflict("candidate IDs must be unique")
        with self.repository.connect() as c:
            c.execute("BEGIN")
            return self._digest(self._candidates(c, client_id, job_ids))

    @staticmethod
    def _digest(candidates):
        return digest([(v.job.id, v.evidence_sha256) for v in candidates])

    def _load(self, c, batch_id):
        row = c.execute("SELECT * FROM daily_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise BatchConflict("batch not found")
        items = c.execute(
            "SELECT * FROM daily_batch_items WHERE batch_id=? ORDER BY ordinal", (batch_id,)
        ).fetchall()
        return DailyBatchResult(
            batch_id=batch_id,
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
                if old["request_json"] != request_json:
                    raise BatchConflict("idempotency key reused with incompatible request")
                return self._load(c, old["batch_id"])
            candidates = self._candidates(c, request.client_id, request.candidate_job_ids)
            if self._digest(candidates) != request.evidence_sha256:
                raise BatchConflict("candidate evidence changed; capture an explicit new scope")
            scoped = [
                BatchCandidate(
                    v.job,
                    v.match,
                    v.group_id,
                    v.evidence_sha256,
                    v.historical,
                    c.execute(
                        "SELECT 1 FROM group_deliveries WHERE group_id=? AND client_id=? "
                        "AND destination=?",
                        (v.group_id, request.client_id, request.destination),
                    ).fetchone()
                    is not None,
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
                "INSERT INTO daily_batches (batch_id,client_id,destination,idempotency_key,"
                "requested_quota,selected_count,shortfall,status,assembled_at,request_json,"
                "counts_json,dedupe_version) VALUES (?,?,?,?,?,?,?,'prepared',?,?,?,?)",
                (
                    batch_id,
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
            c, result.request.client_id, tuple(i.representative_job_id for i in result.items)
        )
        expected = {i.representative_job_id: i.evidence_sha256 for i in result.items}
        if len({v.group_id for v in candidates}) != len(candidates):
            raise BatchConflict("prepared groups merged; selection cannot be silently changed")
        for v in candidates:
            if (
                v.evidence_sha256 != expected[v.job.id]
                or v.historical
                or v.match.decision
                not in {MatchDecision.STRONG_MATCH, MatchDecision.POSSIBLE_MATCH}
                or c.execute(
                    "SELECT 1 FROM group_deliveries WHERE group_id=? "
                    "AND client_id=? AND destination=?",
                    (v.group_id, result.request.client_id, result.request.destination),
                ).fetchone()
            ):
                raise BatchConflict("prepared evidence or delivery state changed")

    def fail(self, batch_id, error):
        with self.repository.connect() as c:
            c.execute(
                "UPDATE daily_batches SET status='failed',error=? WHERE batch_id=? "
                "AND status!='delivered'",
                (str(error), batch_id),
            )
        return self.get(batch_id)

    def finalize(self, batch_id, plan, inspect, publish):
        """Journal commits before IO; delivery marks commit only after durable file verification."""
        try:
            with self.repository.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                result = self._load(c, batch_id)
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
                for item in result.items:
                    group = c.execute(
                        "SELECT group_id FROM posting_delivery_groups WHERE job_id=?",
                        (item.representative_job_id,),
                    ).fetchone()[0]
                    c.execute(
                        "INSERT OR IGNORE INTO group_deliveries VALUES (?,?,?,?,?)",
                        (
                            group,
                            result.request.client_id,
                            result.request.destination,
                            item.representative_job_id,
                            now,
                        ),
                    )
                    c.execute(
                        "INSERT OR IGNORE INTO exports VALUES (?,?,?,?)",
                        (
                            item.representative_job_id,
                            result.request.client_id,
                            result.request.destination,
                            now,
                        ),
                    )
                c.execute(
                    "UPDATE daily_batches SET status='delivered',delivered_at=?,error=NULL "
                    "WHERE batch_id=?",
                    (now, batch_id),
                )
                return self._load(c, batch_id)
        except (OSError, sqlite3.Error, ValueError, csv.Error) as error:
            return self.fail(batch_id, error)
