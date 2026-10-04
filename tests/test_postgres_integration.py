import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_scout.delivery_destinations import ClientSheetDestinationStore
from job_scout.delivery_profiles import ClientDeliveryProfileStore
from job_scout.domain.daily_batch import DailyBatchCounts, DailyBatchRequest
from job_scout.storage.daily_batches import DailyBatchStore
from job_scout.storage.inventory_runs import InventoryRunStore
from job_scout.storage.migrate_to_postgres import migrate
from job_scout.storage.operator_state import OperatorStateStore
from job_scout.storage.postgres import PostgresRepository
from job_scout.storage.refresh_schedule import InventoryRefreshScheduleStore
from job_scout.storage.source_discovery import SourceDiscoveryStore
from job_scout.storage.sqlite import SQLiteRepository

POSTGRES_URL = os.getenv("JOBSIFT_TEST_POSTGRES_URL", "").strip()


pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="JOBSIFT_TEST_POSTGRES_URL is required for Postgres integration tests",
)


def _bootstrap_schema(url: str) -> None:
    import psycopg

    root = Path(__file__).resolve().parents[1]
    schema = (root / "job_scout/storage/postgres_schema.sql").read_text()
    immutability = (
        root / "job_scout/storage/postgres_operator_immutability.sql"
    ).read_text()
    with psycopg.connect(url, autocommit=True) as connection:
        for statement in schema.split(";"):
            if statement.strip():
                connection.execute(statement)
        connection.execute(immutability)


@pytest.fixture(scope="module")
def repository():
    _bootstrap_schema(POSTGRES_URL)
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

    with (
        pytest.raises(sqlite3.DatabaseError, match="immutable"),
        repository.connect() as connection,
    ):
        connection.execute(
            "UPDATE operator_outcome_events SET value=? WHERE event_id=?",
            ("not_applied", "postgres-ci-event"),
        )


def test_operator_outcome_tables_reject_truncate(repository):
    with (
        pytest.raises(sqlite3.DatabaseError, match="immutable"),
        repository.connect() as connection,
    ):
        connection.execute("TRUNCATE operator_outcome_events CASCADE")

    with (
        pytest.raises(sqlite3.DatabaseError, match="immutable"),
        repository.connect() as connection,
    ):
        connection.execute("TRUNCATE operator_outcome_imports")


@pytest.fixture()
def migration_database():
    import psycopg

    name = "jobsift_migration_ci"
    base = POSTGRES_URL.rsplit("/", 1)[0]
    url = f"{base}/{name}"
    with psycopg.connect(POSTGRES_URL, autocommit=True) as connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        connection.execute(f'CREATE DATABASE "{name}"')
    _bootstrap_schema(url)
    try:
        yield url
    finally:
        with psycopg.connect(POSTGRES_URL, autocommit=True) as connection:
            connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_verified_sqlite_to_postgres_migration_preserves_rows_and_identity_sequences(
    tmp_path,
    migration_database,
):
    import psycopg

    source_path = tmp_path / "source.sqlite3"
    source = SQLiteRepository(source_path)

    InventoryRunStore(source)
    InventoryRefreshScheduleStore(source, schedule_key="migration-ci")
    SourceDiscoveryStore(source)
    DailyBatchStore(source)
    ClientSheetDestinationStore(source)
    ClientDeliveryProfileStore(source)
    OperatorStateStore(source)

    now = datetime(2026, 10, 4, 9, 0, tzinfo=UTC)
    InventoryRunStore(source).create_if_absent(
        run_id="migration-run",
        plan_id="migration-plan",
        started_at=now,
    )
    InventoryRefreshScheduleStore(source, schedule_key="migration-ci").next_due(now=now)
    SourceDiscoveryStore(source).set_cursor(
        discovery_source="migration",
        query_id="q1",
        crawl_id="crawl-1",
        page=0,
        pages=1,
        updated_at=now,
    )
    with source.connect() as connection:
        connection.execute(
            "INSERT INTO collection_runs "
            "(source,target,started_at,completed_at,status,metrics_json,errors_json) "
            "VALUES (?,?,?,?,?,?,?)",
            ("greenhouse", "migration", now.isoformat(), now.isoformat(), "success", "{}", "[]"),
        )
        connection.execute(
            "INSERT INTO historical_imports VALUES (?,?,?,?,?,?)",
            ("import-1", "client-1", "sha", now.isoformat(), "test", 1),
        )
        connection.execute(
            "INSERT INTO historical_job_links "
            "(import_id,client_id,original_url,normalized_url,source,source_board_id,"
            "source_job_id,title,company,operator_status,source_sheet,source_row,imported_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "import-1",
                "client-1",
                "https://example.com/1",
                "https://example.com/1",
                "greenhouse",
                "board",
                "job-1",
                "Engineer",
                "Example",
                "unknown",
                "Sheet1",
                1,
                now.isoformat(),
            ),
        )

    result = migrate(
        source_database=source_path,
        destination_url=migration_database,
    )

    assert result["verified"] is True
    assert result["tables"]["inventory_runs"]["rows"] == 1
    assert result["tables"]["historical_job_links"]["rows"] == 1
    assert result["tables"]["source_discovery_cursors"]["rows"] == 1

    with psycopg.connect(migration_database, autocommit=True) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM inventory_runs WHERE run_id='migration-run'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM source_discovery_cursors "
            "WHERE discovery_source='migration' AND query_id='q1'"
        ).fetchone()[0] == 1
        next_id = connection.execute(
            "INSERT INTO historical_job_links "
            "(import_id,client_id,original_url,normalized_url,source,source_board_id,"
            "source_job_id,title,company,operator_status,source_sheet,source_row,imported_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (
                "import-1",
                "client-1",
                "https://example.com/2",
                "https://example.com/2",
                "greenhouse",
                "board",
                "job-2",
                "Engineer II",
                "Example",
                "unknown",
                "Sheet1",
                2,
                now.isoformat(),
            ),
        ).fetchone()[0]
        assert next_id > 1


def test_batch_review_reads_from_explicit_postgres_backend(repository, monkeypatch):
    request = DailyBatchRequest(
        client_id="postgres-review-client",
        destination="postgres-review-destination",
        idempotency_key="postgres-review-key",
        requested_quota=1,
        evidence_scope_id="postgres-review-scope",
        evaluation_id="postgres-review-eval",
        candidate_job_ids=(),
        evidence_sha256="0" * 64,
    )
    counts = DailyBatchCounts(
        candidate_postings=0,
        match_eligible_postings=0,
        needs_review_postings=0,
        rejected_postings=0,
        match_eligible_groups=0,
        historically_suppressed_groups=0,
        previously_delivered_groups=0,
        duplicate_postings_collapsed=0,
        fresh_eligible_groups=0,
        selected_groups=0,
    )
    with repository.connect() as connection:
        connection.execute(
            "INSERT INTO daily_batches "
            "(batch_id,generation_id,client_id,destination,idempotency_key,"
            "requested_quota,selected_count,shortfall,status,assembled_at,"
            "request_json,counts_json,dedupe_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "postgres-review-batch",
                "postgres-review-generation",
                request.client_id,
                request.destination,
                request.idempotency_key,
                1,
                0,
                1,
                "prepared",
                "2026-10-04T09:00:00+00:00",
                request.model_dump_json(),
                counts.model_dump_json(),
                "test-dedupe",
            ),
        )

    monkeypatch.setenv("JOBSIFT_PERSISTENCE_BACKEND", "postgres")
    monkeypatch.setenv("NEON_DATABASE_URL", POSTGRES_URL)
    result, rows = DailyBatchStore.review_readonly(
        "/tmp/should-not-be-opened.sqlite3",
        "postgres-review-batch",
    )

    assert result.batch_id == "postgres-review-batch"
    assert result.status == "prepared"
    assert result.selected_count == 0
    assert rows == []
