"""Shared source-inventory run membership and provenance."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from job_scout.domain.models import Job

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

    def job_ids(self, run_id: str) -> tuple[str, ...]:
        with self.repository.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT job_id FROM inventory_run_jobs "
                "WHERE run_id=? ORDER BY job_id",
                (run_id,),
            ).fetchall()
        return tuple(row[0] for row in rows)

    def active_job_ids(self, run_id: str) -> tuple[str, ...]:
        """Return only full, active payloads that are valid for client evaluation."""
        with self.repository.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT r.job_id FROM inventory_run_jobs r "
                "JOIN jobs j ON j.id=r.job_id "
                "WHERE r.run_id=? AND j.lifecycle!='closed' ORDER BY r.job_id",
                (run_id,),
            ).fetchall()
        return tuple(row[0] for row in rows)

    def target_job_ids(self, run_id: str, target_identity: str) -> tuple[str, ...]:
        with self.repository.connect() as connection:
            rows = connection.execute(
                "SELECT job_id FROM inventory_run_jobs "
                "WHERE run_id=? AND target_identity=? ORDER BY job_id",
                (run_id, target_identity),
            ).fetchall()
        return tuple(row[0] for row in rows)
