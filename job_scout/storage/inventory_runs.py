"""Shared source-inventory run membership and provenance."""

from __future__ import annotations

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
  run_id TEXT NOT NULL REFERENCES inventory_runs(run_id),
  job_id TEXT NOT NULL REFERENCES jobs(id),
  target_identity TEXT NOT NULL,
  PRIMARY KEY(run_id, job_id, target_identity)
);
CREATE INDEX IF NOT EXISTS ix_inventory_run_jobs_run
  ON inventory_run_jobs(run_id, job_id);
"""


class InventoryRunStore:
    def __init__(self, repository):
        self.repository = repository
        with repository.connect() as connection:
            connection.executescript(INVENTORY_RUN_SCHEMA)

    def create(self, *, run_id: str, plan_id: str, started_at: datetime) -> None:
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO inventory_runs "
                "(run_id,plan_id,started_at,completed_at,status) VALUES (?,?,?,?,?)",
                (run_id, plan_id, started_at.isoformat(), None, "running"),
            )

    def add_jobs(
        self,
        *,
        run_id: str,
        target_identity: str,
        jobs: list[Job],
    ) -> int:
        if not jobs:
            return 0
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "INSERT OR IGNORE INTO inventory_run_jobs "
                "(run_id,job_id,target_identity) VALUES (?,?,?)",
                [(run_id, job.id, target_identity) for job in jobs],
            )
        return len(jobs)

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

    def target_job_ids(self, run_id: str, target_identity: str) -> tuple[str, ...]:
        with self.repository.connect() as connection:
            rows = connection.execute(
                "SELECT job_id FROM inventory_run_jobs "
                "WHERE run_id=? AND target_identity=? ORDER BY job_id",
                (run_id, target_identity),
            ).fetchall()
        return tuple(row[0] for row in rows)
