from job_scout.storage.postgres import POSTGRES_SCHEMA_VERSION, translate_sql


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
