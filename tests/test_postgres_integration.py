import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_scout.storage.inventory_runs import InventoryRunStore
from job_scout.storage.postgres import PostgresRepository
from job_scout.storage.refresh_schedule import InventoryRefreshScheduleStore


POSTGRES_URL = os.getenv("JOBSIFT_TEST_POSTGRES_URL", "").strip()


pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="JOBSIFT_TEST_POSTGRES_URL is required for Postgres integration tests",
)


def _bootstrap_schema() -> None:
    import psycopg

    root = Path(__file__).resolve().parents[1]
    schema = (root / "job_scout/storage/postgres_schema.sql").read_text()
    with psycopg.connect(POSTGRES_URL, autocommit=True) as connection:
        for statement in schema.split(";"):
            if statement.strip():
                connection.execute(statement)

        connection.execute(
            """
            CREATE OR REPLACE FUNCTION jobsift_reject_operator_state_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
              RAISE EXCEPTION 'operator outcome state is immutable';
            END;
            $$
            """
        )
        for statement in (
            "CREATE TRIGGER outcome_events_no_update "
            "BEFORE UPDATE ON operator_outcome_events FOR EACH ROW "
            "EXECUTE FUNCTION jobsift_reject_operator_state_mutation()",
            "CREATE TRIGGER outcome_events_no_delete "
            "BEFORE DELETE ON operator_outcome_events FOR EACH ROW "
            "EXECUTE FUNCTION jobsift_reject_operator_state_mutation()",
            "CREATE TRIGGER outcome_imports_no_update "
            "BEFORE UPDATE ON operator_outcome_imports FOR EACH ROW "
            "EXECUTE FUNCTION jobsift_reject_operator_state_mutation()",
            "CREATE TRIGGER outcome_imports_no_delete "
            "BEFORE DELETE ON operator_outcome_imports FOR EACH ROW "
            "EXECUTE FUNCTION jobsift_reject_operator_state_mutation()",
        ):
            connection.execute(statement)


@pytest.fixture(scope="module")
def repository():
    _bootstrap_schema()
    return PostgresRepository(POSTGRES_URL)


def test_postgres_repository_preserves_scheduler_and_run_idempotency(repository):
    schedule = InventoryRefreshScheduleStore(repository, schedule_key="postgres-ci")
    now = datetime(2026, 10, 4, 9, 0, tzinfo=UTC)

    due = schedule.next_due(now=now)
    assert due.should_run is True
    assert due.cohort is not None
    assert schedule.mark_completed(cohort=due.cohort, completed_at=now) is True
    assert schedule.next_due(now=now).should_run is False

    runs = InventoryRunStore(repository)
    assert runs.create_if_absent(
        run_id="postgres-ci-run",
        plan_id="postgres-ci-plan",
        started_at=now,
    ) is True
    assert runs.create_if_absent(
        run_id="postgres-ci-run",
        plan_id="postgres-ci-plan",
        started_at=now,
    ) is False
    runs.finish(run_id="postgres-ci-run", status="success", completed_at=now)
    assert runs.get("postgres-ci-run").status == "success"


def test_postgres_adapter_supports_parameterized_table_detection(repository):
    with repository.connect() as connection:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            ("daily_batches",),
        ).fetchone()
    assert row is not None


def test_operator_outcome_rows_remain_immutable(repository):
    with repository.connect() as connection:
        connection.execute(
            "INSERT INTO operator_outcome_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "postgres-ci-event",
                "postgres-ci-client",
                "posting",
                "job",
                "applied",
                1,
                "system",
                "postgres-ci",
                "2026-10-04T09:00:00+00:00",
                "record_outcome",
                "key",
                "sha",
                "postgres-ci",
                "{}",
            ),
        )

    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        with repository.connect() as connection:
            connection.execute(
                "UPDATE operator_outcome_events SET value=? WHERE event_id=?",
                ("not_applied", "postgres-ci-event"),
            )
