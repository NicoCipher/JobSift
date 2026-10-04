"""Persistent logical-hour scheduler for production inventory refreshes."""

from dataclasses import dataclass
from datetime import UTC, datetime

REFRESH_SCHEDULE_SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory_refresh_schedule_state (
  schedule_key TEXT PRIMARY KEY,
  last_completed_cohort INTEGER NOT NULL CHECK(last_completed_cohort >= -1),
  updated_at TEXT NOT NULL
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
                connection.execute(
                    "INSERT INTO inventory_refresh_schedule_state "
                    "(schedule_key,last_completed_cohort,updated_at) VALUES (?,?,?)",
                    (self.schedule_key, cohort, completed.isoformat()),
                )
                return True
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
