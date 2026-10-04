from datetime import UTC, datetime, timedelta

import pytest

from job_scout import inventory_refresh
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
    repository = SQLiteRepository(tmp_path / "first-failure.sqlite3")
    store = InventoryRefreshScheduleStore(repository)
    first_at = datetime(2026, 10, 4, 4, 17, tzinfo=UTC)

    first = store.next_due(now=first_at)
    next_hour = store.next_due(now=first_at + timedelta(hours=1, minutes=5))

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
