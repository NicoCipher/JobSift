"""Shared source-inventory run membership and provenance."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from job_scout.domain.models import Job
from job_scout.retention import retention_basis

INVENTORY_RUN_SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory_runs (
  run_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  status TEXT NOT NULL CHECK(status IN ('running','success','partial','failure'))
);
CREATE INDEX IF NOT EXISTS ix_inventory_runs_plan_started
  ON inventory_runs(plan_id, started_at);
CREATE TABLE IF NOT EXISTS inventory_run_jobs (
  run_id TEXT NOT NULL REFERENCES inventory_runs(run_id) ON DELETE CASCADE,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  target_identity TEXT NOT NULL,
  PRIMARY KEY(run_id, job_id, target_identity)
);
CREATE INDEX IF NOT EXISTS ix_inventory_run_jobs_run
  ON inventory_run_jobs(run_id, job_id);
CREATE TABLE IF NOT EXISTS inventory_target_observations (
  run_id TEXT NOT NULL REFERENCES inventory_runs(run_id) ON DELETE CASCADE,
  target_identity TEXT NOT NULL,
  source TEXT NOT NULL,
  started_at TEXT NOT NULL,
  completed_at TEXT NOT NULL,
  status TEXT NOT NULL,
  runtime_ms INTEGER NOT NULL CHECK(runtime_ms >= 0),
  raw_postings_received INTEGER NOT NULL CHECK(raw_postings_received >= 0),
  normalized_jobs INTEGER NOT NULL CHECK(normalized_jobs >= 0),
  postings_with_trustworthy_timestamps INTEGER NOT NULL
    CHECK(postings_with_trustworthy_timestamps >= 0),
  postings_at_most_24h_old INTEGER NOT NULL
    CHECK(postings_at_most_24h_old >= 0),
  PRIMARY KEY(run_id, target_identity)
);
CREATE INDEX IF NOT EXISTS ix_inventory_target_observations_target_completed
  ON inventory_target_observations(target_identity, completed_at);
CREATE INDEX IF NOT EXISTS ix_inventory_target_observations_source_completed
  ON inventory_target_observations(source, completed_at);
CREATE TABLE IF NOT EXISTS inventory_target_coverage_state (
  target_identity TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  first_observed_at TEXT NOT NULL,
  previous_observed_at TEXT,
  last_observed_at TEXT NOT NULL,
  observation_count INTEGER NOT NULL CHECK(observation_count >= 1),
  last_run_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_inventory_target_coverage_state_source_last
  ON inventory_target_coverage_state(source, last_observed_at);
"""


@dataclass(frozen=True)
class InventoryRunRecord:
    run_id: str
    plan_id: str
    started_at: datetime
    completed_at: datetime | None
    status: str


class InventoryRunStore:
    def __init__(self, repository):
        self.repository = repository
        with repository.connect() as connection:
            connection.executescript(INVENTORY_RUN_SCHEMA)

    @staticmethod
    def _record(row) -> InventoryRunRecord:
        return InventoryRunRecord(
            run_id=row["run_id"],
            plan_id=row["plan_id"],
            started_at=datetime.fromisoformat(row["started_at"]),
            completed_at=(
                datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None
            ),
            status=row["status"],
        )

    def get(self, run_id: str) -> InventoryRunRecord | None:
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT run_id,plan_id,started_at,completed_at,status "
                "FROM inventory_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return self._record(row) if row is not None else None

    def create(self, *, run_id: str, plan_id: str, started_at: datetime) -> None:
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO inventory_runs "
                "(run_id,plan_id,started_at,completed_at,status) VALUES (?,?,?,?,?)",
                (run_id, plan_id, started_at.isoformat(), None, "running"),
            )

    def create_if_absent(self, *, run_id: str, plan_id: str, started_at: datetime) -> bool:
        """Create a deterministic run receipt, or verify the existing receipt."""
        started = started_at.isoformat()
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            inserted = connection.execute(
                "INSERT OR IGNORE INTO inventory_runs "
                "(run_id,plan_id,started_at,completed_at,status) VALUES (?,?,?,?,?)",
                (run_id, plan_id, started, None, "running"),
            ).rowcount
            row = connection.execute(
                "SELECT run_id,plan_id,started_at,completed_at,status "
                "FROM inventory_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("inventory run receipt was not created")
        if row["plan_id"] != plan_id or row["started_at"] != started:
            raise ValueError("inventory run identity collision")
        return inserted == 1

    def add_memberships(
        self,
        *,
        run_id: str,
        memberships: Iterable[tuple[str, str]],
    ) -> int:
        """Attach target/job pairs to one run in a single transaction."""
        rows = [(run_id, job_id, target_identity) for target_identity, job_id in memberships]
        if not rows:
            return 0
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "INSERT OR IGNORE INTO inventory_run_jobs "
                "(run_id,job_id,target_identity) VALUES (?,?,?)",
                rows,
            )
        return len(rows)

    def add_jobs(
        self,
        *,
        run_id: str,
        target_identity: str,
        jobs: list[Job],
    ) -> int:
        return self.add_memberships(
            run_id=run_id,
            memberships=((target_identity, job.id) for job in jobs),
        )

    def persist_jobs_and_finish(
        self,
        *,
        run_id: str,
        jobs: Iterable[Job],
        memberships: Iterable[tuple[str, Job]],
        stale_jobs: Iterable[Job] = (),
        target_observations: Iterable[
            tuple[str, str, str, datetime, datetime, int, int, int, int, int]
        ] = (),
        pruned_at: datetime | None = None,
        status: str,
        completed_at: datetime,
    ) -> None:
        """Persist one validated fan-in payload and finish its running receipt atomically."""
        if status not in {"success", "partial", "failure"}:
            raise ValueError("invalid inventory run status")
        job_values = list(jobs)
        membership_values = list(memberships)
        stale_values = list(stale_jobs)
        observation_values = list(target_observations)
        now = datetime.now(UTC).isoformat()
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.repository._record_pruned_identities_in_connection(
                connection,
                stale_values,
                pruned_at=pruned_at,
            )
            for job in job_values:
                self.repository._upsert_job_in_connection(connection, job, now)
            if membership_values:
                connection.executemany(
                    "INSERT OR IGNORE INTO inventory_run_jobs "
                    "(run_id,job_id,target_identity) VALUES (?,?,?)",
                    [
                        (run_id, job.id, target_identity)
                        for target_identity, job in membership_values
                    ],
                )
            if observation_values:
                connection.executemany(
                    "INSERT INTO inventory_target_observations "
                    "(run_id,target_identity,source,started_at,completed_at,status,"
                    "runtime_ms,raw_postings_received,normalized_jobs,"
                    "postings_with_trustworthy_timestamps,"
                    "postings_at_most_24h_old) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            run_id,
                            target_identity,
                            source,
                            started_at.isoformat(),
                            completed.isoformat(),
                            target_status,
                            runtime_ms,
                            raw_postings_received,
                            normalized_jobs,
                            timestamped,
                            fresh_24h,
                        )
                        for (
                            target_identity,
                            source,
                            target_status,
                            started_at,
                            completed,
                            runtime_ms,
                            raw_postings_received,
                            normalized_jobs,
                            timestamped,
                            fresh_24h,
                        ) in observation_values
                    ],
                )
                connection.executemany(
                    "INSERT INTO inventory_target_coverage_state "
                    "(target_identity,source,first_observed_at,previous_observed_at,"
                    "last_observed_at,observation_count,last_run_id) "
                    "VALUES (?,?,?,NULL,?,1,?) "
                    "ON CONFLICT(target_identity) DO UPDATE SET "
                    "source=excluded.source,"
                    "previous_observed_at=CASE WHEN excluded.last_observed_at>"
                    "inventory_target_coverage_state.last_observed_at "
                    "THEN inventory_target_coverage_state.last_observed_at "
                    "ELSE inventory_target_coverage_state.previous_observed_at END,"
                    "last_observed_at=CASE WHEN excluded.last_observed_at>"
                    "inventory_target_coverage_state.last_observed_at "
                    "THEN excluded.last_observed_at "
                    "ELSE inventory_target_coverage_state.last_observed_at END,"
                    "observation_count=CASE WHEN excluded.last_observed_at>"
                    "inventory_target_coverage_state.last_observed_at "
                    "THEN inventory_target_coverage_state.observation_count+1 "
                    "ELSE inventory_target_coverage_state.observation_count END,"
                    "last_run_id=CASE WHEN excluded.last_observed_at>"
                    "inventory_target_coverage_state.last_observed_at "
                    "THEN excluded.last_run_id "
                    "ELSE inventory_target_coverage_state.last_run_id END",
                    [
                        (
                            target_identity,
                            source,
                            completed.isoformat(),
                            completed.isoformat(),
                            run_id,
                        )
                        for (
                            target_identity,
                            source,
                            _target_status,
                            _started_at,
                            completed,
                            _runtime_ms,
                            _raw_postings_received,
                            _normalized_jobs,
                            _timestamped,
                            _fresh_24h,
                        ) in observation_values
                    ],
                )
            updated = connection.execute(
                "UPDATE inventory_runs SET status=?,completed_at=? "
                "WHERE run_id=? AND status='running'",
                (status, completed_at.isoformat(), run_id),
            ).rowcount
            if updated != 1:
                raise ValueError("running inventory run not found")

    def finish(
        self,
        *,
        run_id: str,
        status: str,
        completed_at: datetime,
    ) -> None:
        if status not in {"success", "partial", "failure"}:
            raise ValueError("invalid inventory run status")
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE inventory_runs SET status=?,completed_at=? WHERE run_id=?",
                (status, completed_at.isoformat(), run_id),
            ).rowcount
            if updated != 1:
                raise ValueError("inventory run not found")

    def membership_count(self, run_id: str) -> int:
        """Return the persisted target/job membership count for one run."""
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM inventory_run_jobs WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return int(row[0])

    def job_ids(self, run_id: str) -> tuple[str, ...]:
        with self.repository.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT job_id FROM inventory_run_jobs "
                "WHERE run_id=? ORDER BY job_id",
                (run_id,),
            ).fetchall()
        return tuple(row[0] for row in rows)

    def active_job_ids(
        self,
        run_id: str,
        *,
        retention_hours: int,
        evaluated_at: datetime | None = None,
    ) -> tuple[str, ...]:
        """Return full active payloads inside the shared retention window."""
        if retention_hours < 1:
            raise ValueError("retention_hours must be at least 1")
        evaluation_time = evaluated_at or datetime.now(UTC)
        evaluation_time = (
            evaluation_time.replace(tzinfo=UTC)
            if evaluation_time.tzinfo is None
            else evaluation_time.astimezone(UTC)
        )
        cutoff = evaluation_time - timedelta(hours=retention_hours)
        active: list[str] = []
        with self.repository.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT r.job_id,j.payload_json,j.first_seen_at,"
                "e.posted_at AS retention_posted_at FROM inventory_run_jobs r "
                "JOIN jobs j ON j.id=r.job_id "
                "LEFT JOIN job_retention_evidence e ON e.job_id=j.id "
                "WHERE r.run_id=? AND j.lifecycle!='closed' "
                "ORDER BY r.job_id",
                (run_id,),
            ).fetchall()
        for row in rows:
            job = Job.model_validate_json(row["payload_json"])
            retention_posted_at = (
                datetime.fromisoformat(row["retention_posted_at"])
                if row["retention_posted_at"]
                else None
            )
            basis = retention_basis(
                retention_posted_at=retention_posted_at,
                posted_at=job.posted_at,
                first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
            )
            if basis >= cutoff:
                active.append(row["job_id"])
        return tuple(active)

    def target_job_ids(self, run_id: str, target_identity: str) -> tuple[str, ...]:
        with self.repository.connect() as connection:
            rows = connection.execute(
                "SELECT job_id FROM inventory_run_jobs "
                "WHERE run_id=? AND target_identity=? ORDER BY job_id",
                (run_id, target_identity),
            ).fetchall()
        return tuple(row[0] for row in rows)
