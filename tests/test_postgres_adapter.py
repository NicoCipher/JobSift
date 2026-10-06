from datetime import UTC, datetime

from job_scout.domain.models import Job, JobLifecycle
from job_scout.normalization.core import content_fingerprint
from job_scout.storage.postgres import (
    POSTGRES_SCHEMA_VERSION,
    PostgresRepository,
    _chunks,
    translate_sql,
)
from job_scout.storage.sqlite import SQLiteRepository


def test_begin_immediate_uses_transaction_scoped_advisory_lock():
    sql, params = translate_sql("BEGIN IMMEDIATE")

    assert sql == "SELECT pg_advisory_xact_lock(%s)"
    assert params and len(params) == 1


def test_insert_or_ignore_becomes_postgres_conflict_noop():
    sql, params = translate_sql(
        "INSERT OR IGNORE INTO inventory_runs "
        "(run_id,plan_id,started_at,completed_at,status) VALUES (?,?,?,?,?)"
    )

    assert sql == (
        "INSERT INTO inventory_runs "
        "(run_id,plan_id,started_at,completed_at,status) VALUES (%s,%s,%s,%s,%s) "
        "ON CONFLICT DO NOTHING"
    )
    assert params is None


def test_job_match_replace_becomes_explicit_upsert():
    sql, params = translate_sql(
        "INSERT OR REPLACE INTO job_matches VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    )

    assert "INSERT INTO job_matches VALUES (%s, %s, %s, %s, %s, %s, %s, %s)" in sql
    assert "ON CONFLICT (job_id,client_id) DO UPDATE SET" in sql
    assert "matcher_version=excluded.matcher_version" in sql
    assert params is None


def test_sqlite_table_introspection_maps_to_information_schema():
    sql, params = translate_sql("PRAGMA table_info(daily_batches)")

    assert "information_schema.columns" in sql
    assert "daily_batches" in sql
    assert params is None

    sql, params = translate_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_batches'"
    )
    assert "information_schema.tables" in sql
    assert params == ("daily_batches",)


def test_identity_ledger_scalar_max_maps_to_greatest():
    sql, _ = translate_sql(
        "UPDATE job_identity_ledger SET "
        "was_delivered=MAX(job_identity_ledger.was_delivered,excluded.was_delivered)"
    )

    assert "GREATEST(" in sql
    assert "MAX(" not in sql


def test_postgres_schema_version_is_explicit():
    assert POSTGRES_SCHEMA_VERSION == "postgres-v1"


def test_qmark_translation_escapes_literal_percent_for_psycopg():
    sql, params = translate_sql(
        "SELECT 1 FROM daily_batches WHERE client_id=? "
        "AND error LIKE 'prepared posting is no longer fresh at delivery:%'"
    )

    assert "client_id=%s" in sql
    assert "delivery:%%'" in sql
    assert params is None


class _EmptyCursor:
    rowcount = 0

    def fetchall(self):
        return []

    def fetchone(self):
        return None


class _RecordingConnection:
    is_postgres = True

    def __init__(self):
        self.execute_calls = []
        self.executemany_calls = []

    def execute(self, statement, parameters=()):
        values = tuple(parameters)
        self.execute_calls.append((statement, values))
        return _EmptyCursor()

    def executemany(self, statement, parameters):
        rows = list(parameters)
        self.executemany_calls.append((statement, rows))
        return _EmptyCursor()


class _CapacityJob:
    def __init__(self, index: int):
        now = datetime(2026, 10, 6, tzinfo=UTC)
        self.id = f"job-{index}"
        self.source = "greenhouse"
        self.source_job_id = str(index)
        self.source_board_id = f"board-{index // 100}"
        self.canonical_url = f"https://example.com/jobs/{index}"
        self.job_url = None
        self.apply_url = None
        self.content_fingerprint = f"fingerprint-{index}"
        self.discovered_at = now
        self.last_seen_at = now
        self.description_text = None
        self.eligible_countries = set()
        self.country = None

    def model_dump_json(self):
        return "{}"


def test_postgres_batch_window_bounds_20k_parameter_sets():
    chunks = list(_chunks(list(range(20_000))))

    assert len(chunks) == 40
    assert max(len(chunk) for chunk in chunks) == 500


def test_postgres_upsert_20k_uses_bounded_round_trips():
    repository = object.__new__(PostgresRepository)
    connection = _RecordingConnection()
    jobs = [_CapacityJob(index) for index in range(20_000)]
    repository._existing_jobs_in_connection = lambda _connection, _jobs: {}
    grouped = []
    repository._assign_groups_bulk_in_connection = (
        lambda _connection, values: grouped.append(len(values))
    )

    states = repository._upsert_jobs_in_connection(
        connection,
        jobs,
        datetime(2026, 10, 6, tzinfo=UTC).isoformat(),
    )

    assert len(states) == 20_000
    assert set(states.values()) == {JobLifecycle.NEW}
    assert grouped == [20_000]
    assert len(connection.executemany_calls) == 1
    assert len(connection.executemany_calls[0][1]) == 20_000
    retention_deletes = [
        call for call in connection.execute_calls
        if call[0].startswith("DELETE FROM job_retention_evidence")
    ]
    assert len(retention_deletes) == 40
    assert max(len(parameters) for _, parameters in retention_deletes) <= 500


def test_postgres_group_assignment_20k_stays_set_oriented():
    repository = object.__new__(PostgresRepository)
    connection = _RecordingConnection()
    jobs = [_CapacityJob(index) for index in range(20_000)]

    repository._assign_groups_bulk_in_connection(connection, jobs)

    reads = [
        call for call in connection.execute_calls
        if call[0].startswith("SELECT ")
    ]
    key_deletes = [
        call for call in connection.execute_calls
        if call[0].startswith("DELETE FROM delivery_keys")
    ]
    assert len(reads) == 120
    assert len(key_deletes) == 40
    assert len(connection.executemany_calls) == 3
    assert sorted(len(rows) for _, rows in connection.executemany_calls) == [
        20_000,
        20_000,
        20_000,
    ]
    assert max(len(parameters) for _, parameters in reads) <= 1_000


def _delivery_order_job(job_id: str, *, apply_url: str) -> Job:
    now = datetime(2026, 10, 6, tzinfo=UTC)
    description = "Build reliable production software."
    return Job(
        id=job_id,
        source="greenhouse",
        source_job_id=job_id,
        source_board_id="capacity-order",
        title="Software Engineer",
        company="Example",
        description_text=description,
        job_url=f"https://example.com/jobs/{job_id}",
        apply_url=apply_url,
        canonical_url=f"https://example.com/jobs/{job_id}",
        discovered_at=now,
        last_seen_at=now,
        content_fingerprint=content_fingerprint(
            title="Software Engineer",
            description=description,
            location=None,
            employment_type=None,
        ),
    )


def _group_map(repository: SQLiteRepository) -> dict[str, str]:
    with repository.connect() as connection:
        rows = connection.execute(
            "SELECT job_id,group_id FROM posting_delivery_groups ORDER BY job_id"
        ).fetchall()
    return {row["job_id"]: row["group_id"] for row in rows}


def _run_legacy_group_updates(
    repository: SQLiteRepository,
    jobs: list[Job],
) -> dict[str, str]:
    with repository.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for job in jobs:
            repository._assign_group(connection, job)
    return _group_map(repository)


def _run_bulk_group_updates(
    repository: SQLiteRepository,
    jobs: list[Job],
) -> dict[str, str]:
    postgres = object.__new__(PostgresRepository)
    with repository.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        postgres._assign_groups_bulk_in_connection(connection, jobs)
    return _group_map(repository)


def test_postgres_group_merge_20k_uses_bounded_round_trips():
    repository = object.__new__(PostgresRepository)
    connection = _RecordingConnection()
    merge_map = {
        f"group-{index:05d}": "group-root"
        for index in range(20_000)
    }

    repository._merge_delivery_groups_bulk_in_connection(
        connection,
        merge_map,
    )

    assert len(connection.execute_calls) == 160
    assert max(len(parameters) for _, parameters in connection.execute_calls) <= 1_000


def test_postgres_bulk_upsert_preserves_duplicate_provider_identity_sequence():
    repository = object.__new__(PostgresRepository)
    connection = _RecordingConnection()
    first = _delivery_order_job(
        "duplicate-first",
        apply_url="https://apply.example.com/postings/first",
    )
    second = first.model_copy(
        update={
            "id": "duplicate-second",
            "apply_url": "https://apply.example.com/postings/second",
            "content_fingerprint": "changed-fingerprint",
        }
    )
    repository._existing_jobs_in_connection = lambda _connection, _jobs: {}

    states = repository._upsert_jobs_in_connection(
        connection,
        [first, second],
        datetime(2026, 10, 6, tzinfo=UTC).isoformat(),
    )

    assert second.id == first.id
    assert states == {first.id: JobLifecycle.CHANGED}
    group_writes = [
        rows
        for statement, rows in connection.executemany_calls
        if statement.startswith("INSERT INTO posting_delivery_groups")
    ]
    assert len(group_writes) == 1
    assert group_writes[0] == [(first.id, group_writes[0][0][1])]
    key_writes = [
        rows
        for statement, rows in connection.executemany_calls
        if statement.startswith("INSERT INTO delivery_keys")
    ]
    assert len(key_writes) == 1
    assert all(row[0] == first.id for row in key_writes[0])
    assert any(row[2].endswith("/second") for row in key_writes[0])
    assert not any(row[2].endswith("/first") for row in key_writes[0])


def test_bulk_group_assignment_matches_legacy_when_changed_key_disappears_first(tmp_path):
    shared = "https://apply.example.com/postings/shared"
    old_a = _delivery_order_job("a", apply_url=shared)
    old_b = _delivery_order_job(
        "b", apply_url="https://apply.example.com/postings/b-old"
    )
    new_a = old_a.model_copy(
        update={"apply_url": "https://apply.example.com/postings/a-new"}
    )
    new_b = old_b.model_copy(update={"apply_url": shared})

    legacy = SQLiteRepository(tmp_path / "legacy-a-first.sqlite3")
    bulk = SQLiteRepository(tmp_path / "bulk-a-first.sqlite3")
    for repository in (legacy, bulk):
        repository.upsert_job(old_a.model_copy(deep=True))
        repository.upsert_job(old_b.model_copy(deep=True))

    legacy_groups = _run_legacy_group_updates(
        legacy,
        [new_a.model_copy(deep=True), new_b.model_copy(deep=True)],
    )
    bulk_groups = _run_bulk_group_updates(
        bulk,
        [new_a.model_copy(deep=True), new_b.model_copy(deep=True)],
    )

    assert bulk_groups == legacy_groups
    assert bulk_groups["a"] != bulk_groups["b"]


def test_bulk_group_assignment_matches_legacy_when_shared_key_is_seen_first(tmp_path):
    shared = "https://apply.example.com/postings/shared"
    old_a = _delivery_order_job("a", apply_url=shared)
    old_b = _delivery_order_job(
        "b", apply_url="https://apply.example.com/postings/b-old"
    )
    new_a = old_a.model_copy(
        update={"apply_url": "https://apply.example.com/postings/a-new"}
    )
    new_b = old_b.model_copy(update={"apply_url": shared})

    legacy = SQLiteRepository(tmp_path / "legacy-b-first.sqlite3")
    bulk = SQLiteRepository(tmp_path / "bulk-b-first.sqlite3")
    for repository in (legacy, bulk):
        repository.upsert_job(old_a.model_copy(deep=True))
        repository.upsert_job(old_b.model_copy(deep=True))

    legacy_groups = _run_legacy_group_updates(
        legacy,
        [new_b.model_copy(deep=True), new_a.model_copy(deep=True)],
    )
    bulk_groups = _run_bulk_group_updates(
        bulk,
        [new_b.model_copy(deep=True), new_a.model_copy(deep=True)],
    )

    assert bulk_groups == legacy_groups
    assert bulk_groups["a"] == bulk_groups["b"]
