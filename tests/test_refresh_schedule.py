from datetime import UTC, datetime, timedelta

import pytest

from job_scout import inventory_refresh
from job_scout.storage.inventory_runs import InventoryRunStore
from job_scout.storage.refresh_schedule import InventoryRefreshScheduleStore
from job_scout.storage.sqlite import SQLiteRepository


def test_schedule_retries_unfinished_hour_and_noops_after_completion(tmp_path):
    repository = SQLiteRepository(tmp_path / "schedule.sqlite3")
    store = InventoryRefreshScheduleStore(repository)
    now = datetime(2026, 10, 4, 4, 17, tzinfo=UTC)

    first = store.next_due(now=now)
    retry = store.next_due(now=now + timedelta(minutes=20))

    assert first.should_run is True
    assert first.cohort == store.cohort_for(now)
    assert retry.cohort == first.cohort

    assert store.mark_completed(cohort=first.cohort, completed_at=now) is True
    assert store.mark_completed(cohort=first.cohort, completed_at=now) is False

    done = store.next_due(now=now + timedelta(minutes=40))
    assert done.should_run is False
    assert done.cohort is None


def test_schedule_catches_up_oldest_missed_hour_first(tmp_path):
    repository = SQLiteRepository(tmp_path / "catchup.sqlite3")
    store = InventoryRefreshScheduleStore(repository)
    start = datetime(2026, 10, 4, 1, 17, tzinfo=UTC)
    cohort = store.cohort_for(start)
    initial = store.next_due(now=start)
    assert initial.cohort == cohort
    assert store.mark_completed(cohort=cohort, completed_at=start) is True

    later = start + timedelta(hours=4)
    due = store.next_due(now=later)

    assert due.current_cohort == cohort + 4
    assert due.cohort == cohort + 1
    assert due.should_run is True

    assert store.mark_completed(
        cohort=cohort + 1,
        completed_at=start + timedelta(hours=1),
    ) is True
    next_due = store.next_due(now=later)
    assert next_due.current_cohort == cohort + 4
    assert next_due.cohort == cohort + 2
    assert next_due.should_run is True


def test_schedule_rejects_completion_gaps(tmp_path):
    repository = SQLiteRepository(tmp_path / "gap.sqlite3")
    store = InventoryRefreshScheduleStore(repository)
    start = datetime(2026, 10, 4, 1, 17, tzinfo=UTC)
    cohort = store.cohort_for(start)
    initial = store.next_due(now=start)
    assert initial.cohort == cohort
    assert store.mark_completed(cohort=cohort, completed_at=start) is True

    with pytest.raises(ValueError, match="must be contiguous"):
        store.mark_completed(cohort=cohort + 2, completed_at=start + timedelta(hours=2))


def test_completion_requires_initialized_scheduler_state(tmp_path):
    repository = SQLiteRepository(tmp_path / "missing-state.sqlite3")
    store = InventoryRefreshScheduleStore(repository)
    cohort = store.cohort_for(datetime(2026, 10, 4, 4, 17, tzinfo=UTC))

    with pytest.raises(RuntimeError, match="requires initialized scheduler state"):
        store.mark_completed(cohort=cohort)


def test_first_failed_cohort_is_not_skipped_when_clock_advances(tmp_path):
    database = tmp_path / "first-failure.sqlite3"
    store = InventoryRefreshScheduleStore(SQLiteRepository(database))
    first_at = datetime(2026, 10, 4, 4, 17, tzinfo=UTC)

    first = store.next_due(now=first_at)

    # Model the next trigger on a fresh runner/process reopening durable state.
    retry_store = InventoryRefreshScheduleStore(SQLiteRepository(database))
    next_hour = retry_store.next_due(now=first_at + timedelta(hours=1, minutes=5))

    assert first.should_run is True
    assert next_hour.should_run is True
    assert next_hour.cohort == first.cohort
    assert next_hour.current_cohort == first.current_cohort + 1


def test_partial_fan_in_keeps_logical_cohort_due(tmp_path):
    repository = SQLiteRepository(tmp_path / "partial.sqlite3")
    store = InventoryRefreshScheduleStore(repository)
    now = datetime(2026, 10, 4, 4, 17, tzinfo=UTC)
    due = store.next_due(now=now)
    report = tmp_path / "report.json"
    report.write_text('{"fan_in":{"status":"partial"}}', encoding="utf-8")

    result = inventory_refresh.complete_scheduled_cohort(
        repository=repository,
        cohort=due.cohort,
        fan_in_report=report,
    )

    assert result["marked_completed"] is False
    assert result["fan_in_status"] == "partial"
    retry = store.next_due(now=now + timedelta(hours=1))
    assert retry.cohort == due.cohort


def test_successful_fan_in_advances_logical_cohort(tmp_path):
    repository = SQLiteRepository(tmp_path / "success.sqlite3")
    store = InventoryRefreshScheduleStore(repository)
    now = datetime(2026, 10, 4, 4, 17, tzinfo=UTC)
    due = store.next_due(now=now)
    report = tmp_path / "report.json"
    report.write_text('{"fan_in":{"status":"success"}}', encoding="utf-8")

    result = inventory_refresh.complete_scheduled_cohort(
        repository=repository,
        cohort=due.cohort,
        fan_in_report=report,
    )

    assert result["marked_completed"] is True
    done = store.next_due(now=now + timedelta(minutes=20))
    assert done.should_run is False



def test_stale_backlog_recovery_skips_old_hours_and_audits_without_deleting_runs(tmp_path):
    repository = SQLiteRepository(tmp_path / "stale-recovery.sqlite3")
    InventoryRunStore(repository)
    store = InventoryRefreshScheduleStore(repository)
    start = datetime(2026, 10, 4, 1, 17, tzinfo=UTC)
    first = store.next_due(now=start)
    assert store.mark_completed(cohort=first.cohort, completed_at=start) is True

    later = start + timedelta(hours=48)
    recovery = store.recover_stale_backlog(now=later, max_lag_hours=24)

    assert recovery["recovered"] is True
    assert recovery["skipped_cohorts"] == 47
    assert recovery["previous_completed_cohort"] == first.cohort
    assert recovery["resumed_before_cohort"] == first.cohort + 48

    due = store.next_due(now=later)
    assert due.cohort == first.cohort + 48
    assert due.should_run is True

    with repository.connect() as connection:
        audit = connection.execute(
            "SELECT previous_completed_cohort,resumed_before_cohort,skipped_cohorts "
            "FROM inventory_refresh_schedule_recovery_audit"
        ).fetchone()
        run_count = connection.execute(
            "SELECT COUNT(*) AS count FROM inventory_runs"
        ).fetchone()["count"]
    assert audit["skipped_cohorts"] == 47
    assert run_count == 0


def test_stale_backlog_recovery_fails_closed_when_inventory_run_is_active(tmp_path):
    repository = SQLiteRepository(tmp_path / "active-recovery.sqlite3")
    runs = InventoryRunStore(repository)
    store = InventoryRefreshScheduleStore(repository)
    start = datetime(2026, 10, 4, 1, 17, tzinfo=UTC)
    first = store.next_due(now=start)
    assert store.mark_completed(cohort=first.cohort, completed_at=start) is True
    runs.create(run_id="active-run", plan_id="active-plan", started_at=start)

    with pytest.raises(RuntimeError, match="inventory run is active"):
        store.recover_stale_backlog(
            now=start + timedelta(hours=48),
            max_lag_hours=24,
        )

    with repository.connect() as connection:
        state = connection.execute(
            "SELECT last_completed_cohort FROM inventory_refresh_schedule_state"
        ).fetchone()
        audit_count = connection.execute(
            "SELECT COUNT(*) AS count FROM inventory_refresh_schedule_recovery_audit"
        ).fetchone()["count"]
    assert state["last_completed_cohort"] == first.cohort
    assert audit_count == 0


def test_stale_backlog_recovery_does_not_skip_recent_missed_hours(tmp_path):
    repository = SQLiteRepository(tmp_path / "recent-recovery.sqlite3")
    store = InventoryRefreshScheduleStore(repository)
    start = datetime(2026, 10, 4, 1, 17, tzinfo=UTC)
    first = store.next_due(now=start)

    recovery = store.recover_stale_backlog(
        now=start + timedelta(hours=12),
        max_lag_hours=24,
    )

    assert recovery["recovered"] is False
    assert recovery["reason"] == "within_lag_limit"
    due = store.next_due(now=start + timedelta(hours=12))
    assert due.cohort == first.cohort


def test_stale_backlog_recovery_initializes_new_schedule_without_skipping(tmp_path):
    repository = SQLiteRepository(tmp_path / "new-recovery.sqlite3")
    store = InventoryRefreshScheduleStore(repository)
    now = datetime(2026, 10, 4, 1, 17, tzinfo=UTC)

    recovery = store.recover_stale_backlog(now=now)

    assert recovery["recovered"] is False
    assert recovery["reason"] == "initialized"
    assert recovery["skipped_cohorts"] == 0
    assert store.next_due(now=now).cohort == store.cohort_for(now)
