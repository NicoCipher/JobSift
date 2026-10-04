"""One-shot, verified SQLite/Turso -> PostgreSQL data migration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from job_scout.storage.postgres import PostgresConnection, PostgresRepository
from job_scout.storage.sqlite import compatible_row_factory

TABLE_KEYS: dict[str, tuple[str, ...]] = {
    "jobs": ("id",),
    "delivery_groups": ("id",),
    "historical_imports": ("id",),
    "inventory_runs": ("run_id",),
    "source_discovery_targets": ("target_identity",),
    "client_sheet_destinations": ("client_id", "destination_id"),
    "client_delivery_profiles": ("client_id", "destination_id"),
    "operator_outcome_events": ("event_id",),
    "collection_runs": ("id",),
    "job_matches": ("job_id", "client_id"),
    "scoped_job_matches": ("job_id", "client_id", "match_scope_id"),
    "exports": ("job_id", "client_id", "destination"),
    "posting_delivery_groups": ("job_id",),
    "delivery_keys": ("job_id", "kind", "value"),
    "job_identity_ledger": ("source", "source_board_id", "source_job_id"),
    "job_retention_evidence": ("job_id",),
    "group_deliveries": ("group_id", "client_id", "destination"),
    "historical_job_links": ("id",),
    "destination_observed_links": ("client_id", "destination", "normalized_url"),
    "historical_blacklist_evidence": ("id",),
    "inventory_run_jobs": ("run_id", "job_id", "target_identity"),
    "inventory_target_observations": ("run_id", "target_identity"),
    "inventory_target_coverage_state": ("target_identity",),
    "inventory_refresh_schedule_state": ("schedule_key",),
    "source_discovery_evidence": ("evidence_id",),
    "source_discovery_health_evidence": ("evidence_id",),
    "source_discovery_cursors": ("discovery_source", "query_id"),
    "daily_batches": ("batch_id",),
    "daily_batch_items": ("batch_id", "ordinal"),
    "daily_batch_candidates": ("batch_id", "job_id"),
    "operator_outcome_imports": ("import_record_id",),
}

IDENTITY_TABLES = {
    "collection_runs": "id",
    "historical_job_links": "id",
    "historical_blacklist_evidence": "id",
}


def _canonical_row(row, columns: tuple[str, ...]) -> bytes:
    values = [row[column] for column in columns]
    return (
        json.dumps(values, ensure_ascii=False, separators=(",", ":"), default=str)
        + "\n"
    ).encode()


def _table_digest(connection, table: str, columns: tuple[str, ...]) -> tuple[int, str]:
    keys = TABLE_KEYS[table]
    order = ",".join(keys)
    selected = ",".join(columns)
    digest = hashlib.sha256()
    count = 0
    for row in connection.execute(
        f"SELECT {selected} FROM {table} ORDER BY {order}"
    ):
        digest.update(_canonical_row(row, columns))
        count += 1
    return count, digest.hexdigest()


def _sqlite_table_columns(connection, table: str) -> tuple[str, ...] | None:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if exists is None:
        return None
    return tuple(
        str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")
    )


def _postgres_table_columns(connection: PostgresConnection, table: str) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=? ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    return tuple(str(row["column_name"]) for row in rows)


@contextmanager
def source_connection(path: Path) -> Iterator[Any]:
    """Open the migration source without ever pushing changes back to Turso."""

    remote_url = os.getenv("TURSO_DATABASE_URL", "").strip()
    auth_token = os.getenv("TURSO_AUTH_TOKEN", "").strip()
    if bool(remote_url) != bool(auth_token):
        raise ValueError(
            "TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be configured together"
        )

    connection = None
    try:
        if remote_url:
            try:
                import turso
                import turso.sync
            except ImportError as error:
                raise RuntimeError(
                    "install job-scout[cloud] for Turso migration access"
                ) from error
            connection = turso.sync.connect(
                str(path),
                remote_url=remote_url,
                auth_token=auth_token,
            )
        else:
            if not path.exists():
                raise FileNotFoundError(path)
            connection = sqlite3.connect(
                path.resolve().as_uri() + "?mode=ro",
                uri=True,
            )
        connection.row_factory = compatible_row_factory
        connection.execute("PRAGMA foreign_keys = ON")
        yield connection
    finally:
        if connection is not None:
            connection.close()


def _assert_destination_empty(connection: PostgresConnection) -> None:
    occupied: list[str] = []
    for table in TABLE_KEYS:
        row = connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
        if row is not None and int(row["count"]) != 0:
            occupied.append(table)
    if occupied:
        raise RuntimeError(
            "Postgres migration destination is not empty: " + ", ".join(occupied)
        )


def _reset_identity_sequences(connection: PostgresConnection) -> None:
    for table, column in IDENTITY_TABLES.items():
        connection.execute(
            f"SELECT setval("
            f"pg_get_serial_sequence('{table}','{column}'),"
            f"COALESCE(MAX({column}),1),"
            f"MAX({column}) IS NOT NULL"
            f") FROM {table}"
        )


def migrate(
    *,
    source_database: Path,
    destination_url: str,
) -> dict[str, Any]:
    """Copy every durable source table atomically and verify exact row digests."""

    report: dict[str, Any] = {
        "source": "turso" if os.getenv("TURSO_DATABASE_URL", "").strip() else "sqlite",
        "tables": {},
        "verified": False,
    }
    destination = PostgresRepository(destination_url)

    with source_connection(source_database) as source:
        with destination.connect() as target:
            target.execute("BEGIN IMMEDIATE")
            _assert_destination_empty(target)

            for table in TABLE_KEYS:
                source_columns = _sqlite_table_columns(source, table)
                if source_columns is None:
                    report["tables"][table] = {
                        "source_present": False,
                        "rows": 0,
                    }
                    continue

                destination_columns = _postgres_table_columns(target, table)
                missing = [column for column in source_columns if column not in destination_columns]
                if missing:
                    raise RuntimeError(
                        f"Postgres table {table} is missing source columns: {missing}"
                    )

                selected = ",".join(source_columns)
                placeholders = ",".join("?" for _ in source_columns)
                rows = source.execute(f"SELECT {selected} FROM {table}")
                inserted = 0
                for row in rows:
                    target.execute(
                        f"INSERT INTO {table} ({selected}) VALUES ({placeholders})",
                        tuple(row[column] for column in source_columns),
                    )
                    inserted += 1

                source_count, source_digest = _table_digest(
                    source,
                    table,
                    source_columns,
                )
                target_count, target_digest = _table_digest(
                    target,
                    table,
                    source_columns,
                )
                if inserted != source_count or target_count != source_count:
                    raise RuntimeError(
                        f"row-count mismatch for {table}: "
                        f"inserted={inserted} source={source_count} target={target_count}"
                    )
                if target_digest != source_digest:
                    raise RuntimeError(f"row-digest mismatch for {table}")

                report["tables"][table] = {
                    "source_present": True,
                    "rows": source_count,
                    "sha256": source_digest,
                }

            _reset_identity_sequences(target)
            report["verified"] = True

    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate JobSift durable state from SQLite/Turso to Postgres"
    )
    parser.add_argument(
        "--source-database",
        type=Path,
        default=Path("/tmp/jobsift-migration.sqlite3"),
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    destination_url = os.getenv("NEON_DATABASE_URL", "").strip()
    if not destination_url:
        raise SystemExit("NEON_DATABASE_URL is required")

    result = migrate(
        source_database=args.source_database,
        destination_url=destination_url,
    )
    payload = json.dumps(result, indent=2, sort_keys=True)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(payload + "\n")
    print(payload)


if __name__ == "__main__":
    main()
