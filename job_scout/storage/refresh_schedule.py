"""Persistent logical-hour scheduler for production inventory refreshes."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

REFRESH_SCHEDULE_SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory_refresh_schedule_state (
  schedule_key TEXT PRIMARY KEY,
  last_completed_cohort INTEGER NOT NULL CHECK(last_completed_cohort >= -1),
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inventory_refresh_schedule_recovery_audit (
  recovery_id TEXT PRIMARY KEY,
  schedule_key TEXT NOT NULL,
  previous_completed_cohort INTEGER NOT NULL,
  resumed_before_cohort INTEGER NOT NULL,
  skipped_cohorts INTEGER NOT NULL CHECK(skipped_cohorts > 0),
  recovered_at TEXT NOT NULL,
  reason TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class ScheduledCohortDecision:
    should_run: bool
    cohort: int | None
    current_cohort: int
    last_completed_cohort: int | None


class InventoryRefreshScheduleStore:
    """Tracks completed logical UTC-hour cohorts independently of trigger timing."""

    def __init__(self, repository, *, schedule_key: str = "hourly-live-inventory") -> None:
        self.repository = repository
        self.schedule_key = schedule_key
        with repository.connect() as connection:
            connection.executescript(REFRESH_SCHEDULE_SCHEMA)

    @staticmethod
    def cohort_for(now: datetime) -> int:
        normalized = now.replace(tzinfo=now.tzinfo or UTC).astimezone(UTC)
        return int(normalized.timestamp() // 3600)

    def recover_stale_backlog(
        self,
        *,
        now: datetime | None = None,
        max_lag_hours: int = 24,
    ) -> dict[str, int | bool | str | None]:
        """Skip obsolete hourly slots without touching inventory data.

        Recovery is deliberately explicit and fails closed if an inventory run
        is still active. The skipped range is retained in an audit table.
        """
        if max_lag_hours < 1:
            raise ValueError("max_lag_hours must be at least 1")

        moment = now or datetime.now(UTC)
        normalized = moment.replace(tzinfo=moment.tzinfo or UTC).astimezone(UTC)
        current = self.cohort_for(normalized)
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT last_completed_cohort FROM inventory_refresh_schedule_state "
                "WHERE schedule_key=?",
                (self.schedule_key,),
            ).fetchone()
            if row is None:
                # First-ever schedule initialization is not backlog recovery.
                connection.execute(
                    "INSERT INTO inventory_refresh_schedule_state "
                    "(schedule_key,last_completed_cohort,updated_at) VALUES (?,?,?)",
                    (self.schedule_key, current - 1, normalized.isoformat()),
                )
                return {
                    "recovered": False,
                    "reason": "initialized",
                    "previous_completed_cohort": None,
                    "resumed_before_cohort": current,
                    "skipped_cohorts": 0,
                }

            last_completed = int(row["last_completed_cohort"])
            lag_hours = current - last_completed
            if lag_hours <= max_lag_hours:
                return {
                    "recovered": False,
                    "reason": "within_lag_limit",
                    "previous_completed_cohort": last_completed,
                    "resumed_before_cohort": None,
                    "skipped_cohorts": 0,
                }

            active = connection.execute(
                "SELECT COUNT(*) AS active_count FROM inventory_runs "
                "WHERE status='running'"
            ).fetchone()
            active_count = int(active["active_count"])
            if active_count:
                raise RuntimeError(
                    "cannot recover stale schedule while an inventory run is active"
                )

            skipped = current - last_completed - 1
            if skipped < 1:
                return {
                    "recovered": False,
                    "reason": "no_obsolete_cohorts",
                    "previous_completed_cohort": last_completed,
                    "resumed_before_cohort": None,
                    "skipped_cohorts": 0,
                }

            connection.execute(
                "UPDATE inventory_refresh_schedule_state "
                "SET last_completed_cohort=?,updated_at=? WHERE schedule_key=?",
                (current - 1, normalized.isoformat(), self.schedule_key),
            )
            connection.execute(
                "INSERT INTO inventory_refresh_schedule_recovery_audit "
                "(recovery_id,schedule_key,previous_completed_cohort,"
                "resumed_before_cohort,skipped_cohorts,recovered_at,reason) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    str(uuid4()),
                    self.schedule_key,
                    last_completed,
                    current,
                    skipped,
                    normalized.isoformat(),
                    "stale_backlog",
                ),
            )

        return {
            "recovered": True,
            "reason": "stale_backlog",
            "previous_completed_cohort": last_completed,
            "resumed_before_cohort": current,
            "skipped_cohorts": skipped,
        }

    def next_due(self, *, now: datetime | None = None) -> ScheduledCohortDecision:
        moment = now or datetime.now(UTC)
        current = self.cohort_for(moment)
        normalized = moment.replace(tzinfo=moment.tzinfo or UTC).astimezone(UTC)
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT last_completed_cohort FROM inventory_refresh_schedule_state "
                "WHERE schedule_key=?",
                (self.schedule_key,),
            ).fetchone()
            if row is None:
                last_completed = current - 1
                connection.execute(
                    "INSERT INTO inventory_refresh_schedule_state "
                    "(schedule_key,last_completed_cohort,updated_at) VALUES (?,?,?)",
                    (self.schedule_key, last_completed, normalized.isoformat()),
                )
            else:
                last_completed = int(row["last_completed_cohort"])
        if last_completed >= current:
            return ScheduledCohortDecision(
                should_run=False,
                cohort=None,
                current_cohort=current,
                last_completed_cohort=last_completed,
            )
        return ScheduledCohortDecision(
            should_run=True,
            cohort=last_completed + 1,
            current_cohort=current,
            last_completed_cohort=last_completed,
        )

    def mark_completed(
        self,
        *,
        cohort: int,
        completed_at: datetime | None = None,
    ) -> bool:
        if cohort < 0:
            raise ValueError("scheduled cohort must be non-negative")
        moment = completed_at or datetime.now(UTC)
        completed = moment.replace(tzinfo=moment.tzinfo or UTC).astimezone(UTC)
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT last_completed_cohort FROM inventory_refresh_schedule_state "
                "WHERE schedule_key=?",
                (self.schedule_key,),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "scheduled cohort completion requires initialized scheduler state"
                )
            last_completed = int(row["last_completed_cohort"])
            if cohort == last_completed:
                return False
            if cohort != last_completed + 1:
                raise ValueError(
                    "scheduled cohort completion must be contiguous; "
                    f"last={last_completed} attempted={cohort}"
                )
            connection.execute(
                "UPDATE inventory_refresh_schedule_state "
                "SET last_completed_cohort=?,updated_at=? WHERE schedule_key=?",
                (cohort, completed.isoformat(), self.schedule_key),
            )
        return True
