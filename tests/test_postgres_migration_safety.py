from types import SimpleNamespace

import pytest

from job_scout.service.errors import ServiceError
from job_scout.service.read_store import ReadStore
from job_scout.storage.migrate_to_postgres import _table_digest


class _RowsConnection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, _sql):
        return self.rows


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _PostgresReadConnection:
    is_postgres = True

    def __init__(self, *, row_count, total_bytes, rows):
        self.row_count = row_count
        self.total_bytes = total_bytes
        self.rows = rows
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if sql.startswith("SELECT COUNT(*) AS row_count"):
            return _Cursor(
                [
                    {
                        "row_count": self.row_count,
                        "total_bytes": self.total_bytes,
                    }
                ]
            )
        return _Cursor(self.rows)


def _read_store(*, max_rows=100, max_bytes=10_000):
    config = SimpleNamespace(
        max_projection_rows=max_rows,
        max_projection_bytes=max_bytes,
    )
    return ReadStore(SimpleNamespace(config=config))


def test_migration_digest_is_independent_of_backend_row_order():
    columns = ("client_id", "destination_id", "status")
    rows = [
        {"client_id": "alpha", "destination_id": "Z", "status": "active"},
        {"client_id": "Alpha", "destination_id": "é", "status": "paused"},
        {"client_id": "βeta", "destination_id": "a", "status": "active"},
    ]

    left = _table_digest(
        _RowsConnection(rows),
        "client_delivery_profiles",
        columns,
    )
    right = _table_digest(
        _RowsConnection(list(reversed(rows))),
        "client_delivery_profiles",
        columns,
    )

    assert left == right
    assert left[0] == 3


def test_postgres_projection_byte_cap_blocks_before_rows_are_materialized():
    store = _read_store(max_bytes=1_000)
    connection = _PostgresReadConnection(
        row_count=1,
        total_bytes=1_001,
        rows=[{"payload_json": "x" * 1_001}],
    )

    with pytest.raises(ServiceError, match="EVIDENCE_SCOPE_UNAVAILABLE"):
        store.bounded(connection, "SELECT payload_json FROM jobs")

    assert len(connection.calls) == 1
    assert connection.calls[0][0].startswith("SELECT COUNT(*) AS row_count")


def test_postgres_projection_byte_cap_allows_bounded_result():
    store = _read_store(max_rows=2, max_bytes=1_000)
    rows = [{"id": "one"}, {"id": "two"}]
    connection = _PostgresReadConnection(
        row_count=2,
        total_bytes=100,
        rows=rows,
    )

    assert store.bounded(connection, "SELECT id FROM jobs") == rows
    assert len(connection.calls) == 2
