"""PostgreSQL persistence adapter for JobSift production.

The application stores timestamps and JSON as text today so Postgres can preserve
the existing deterministic behavior during migration. SQLite remains the local
and test backend. Production selects this repository explicitly through the
storage factory when NEON_DATABASE_URL is configured.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

from job_scout.domain.models import Job
from job_scout.storage.sqlite import SQLiteRepository

POSTGRES_SCHEMA_VERSION = "postgres-v1"
_WRITE_LOCK_KEY = 1246700874
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_REPLACE_CONFLICTS = {
    "job_retention_evidence": (
        "(job_id) DO UPDATE SET posted_at=excluded.posted_at"
    ),
    "posting_delivery_groups": (
        "(job_id) DO UPDATE SET group_id=excluded.group_id"
    ),
    "job_matches": (
        "(job_id,client_id) DO UPDATE SET "
        "decision=excluded.decision,score=excluded.score,"
        "matched_reasons_json=excluded.matched_reasons_json,"
        "rejection_reasons_json=excluded.rejection_reasons_json,"
        "evaluated_at=excluded.evaluated_at,matcher_version=excluded.matcher_version"
    ),
    "scoped_job_matches": (
        "(job_id,client_id,match_scope_id) DO UPDATE SET "
        "decision=excluded.decision,score=excluded.score,"
        "matched_reasons_json=excluded.matched_reasons_json,"
        "rejection_reasons_json=excluded.rejection_reasons_json,"
        "evaluated_at=excluded.evaluated_at,matcher_version=excluded.matcher_version"
    ),
}


class PostgresRow:
    """sqlite3.Row-compatible access over a psycopg dict row."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values
        self._ordered = tuple(values.values())

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._values[key]
        return self._ordered[key]

    def __iter__(self):
        return iter(self._ordered)

    def __len__(self) -> int:
        return len(self._ordered)

    def keys(self):
        return self._values.keys()


class PostgresCursor:
    def __init__(self, cursor) -> None:
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def description(self):
        return self._cursor.description

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None:
            return None
        return PostgresRow(row) if isinstance(row, dict) else row

    def fetchall(self):
        rows = self._cursor.fetchall()
        return [PostgresRow(row) if isinstance(row, dict) else row for row in rows]

    def __iter__(self):
        for row in self._cursor:
            yield PostgresRow(row) if isinstance(row, dict) else row


def _table_info_query(statement: str) -> str | None:
    match = re.fullmatch(
        r"\s*PRAGMA\s+table_info\(([^)]+)\)\s*;?\s*",
        statement,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    table = match.group(1).strip().strip("'\"")
    if not _IDENTIFIER.fullmatch(table):
        raise ValueError("unsafe table name in schema inspection")
    return (
        "SELECT column_name AS name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name="
        f"'{table}' ORDER BY ordinal_position"
    )


def translate_sql(statement: str) -> tuple[str, tuple[Any, ...] | None]:
    """Translate the narrow SQLite dialect used by JobSift into PostgreSQL."""

    sql = statement.strip()
    upper = sql.upper()

    if upper == "BEGIN IMMEDIATE":
        # SQLite serialized every writer. Preserve that correctness property
        # during the first Postgres migration with one transaction-scoped lock.
        return "SELECT pg_advisory_xact_lock(%s)", (_WRITE_LOCK_KEY,)
    if upper == "BEGIN":
        # psycopg opens a transaction on the first command.
        return "SELECT 1", None
    if upper.startswith("PRAGMA FOREIGN_KEYS"):
        return "SELECT 1", None
    if upper.startswith("PRAGMA FOREIGN_KEY_CHECK"):
        # PostgreSQL checks foreign keys on writes; an empty result mirrors a
        # successful SQLite foreign_key_check.
        return "SELECT NULL WHERE FALSE", None

    table_info = _table_info_query(sql)
    if table_info is not None:
        return table_info, None

    normalized = re.sub(r"\s+", " ", sql)
    sqlite_master = re.fullmatch(
        r"SELECT 1 FROM sqlite_master WHERE type='table' AND name='([^']+)'",
        normalized,
        flags=re.IGNORECASE,
    )
    if sqlite_master is not None:
        table = sqlite_master.group(1)
        if not _IDENTIFIER.fullmatch(table):
            raise ValueError("unsafe table name in schema inspection")
        return (
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name=%s",
            (table,),
        )

    ignore = re.match(
        r"^INSERT\s+OR\s+IGNORE\s+INTO\s+",
        sql,
        flags=re.IGNORECASE,
    )
    if ignore is not None:
        sql = re.sub(
            r"^INSERT\s+OR\s+IGNORE\s+INTO\s+",
            "INSERT INTO ",
            sql,
            count=1,
            flags=re.IGNORECASE,
        ).rstrip(";")
        sql += " ON CONFLICT DO NOTHING"

    replace = re.match(
        r"^INSERT\s+OR\s+REPLACE\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\b",
        sql,
        flags=re.IGNORECASE,
    )
    if replace is not None:
        table = replace.group(1)
        conflict = _REPLACE_CONFLICTS.get(table)
        if conflict is None:
            raise ValueError(f"unsupported SQLite REPLACE target for Postgres: {table}")
        sql = re.sub(
            r"^INSERT\s+OR\s+REPLACE\s+INTO\s+",
            "INSERT INTO ",
            sql,
            count=1,
            flags=re.IGNORECASE,
        ).rstrip(";")
        sql += f" ON CONFLICT {conflict}"

    sql = sql.replace(
        "MAX(job_identity_ledger.was_delivered,excluded.was_delivered)",
        "GREATEST(job_identity_ledger.was_delivered,excluded.was_delivered)",
    )
    sql = sql.replace("?", "%s")
    return sql, None


class PostgresConnection:
    """Small DB-API compatibility surface used by existing JobSift stores."""

    def __init__(self, connection) -> None:
        self._connection = connection

    def execute(self, statement: str, parameters: Iterable[Any] = ()) -> PostgresCursor:
        sql, replacement_parameters = translate_sql(statement)
        values = (
            tuple(replacement_parameters)
            if replacement_parameters is not None
            else tuple(parameters)
        )
        cursor = self._connection.cursor()
        cursor.execute(sql, values)
        return PostgresCursor(cursor)

    def executemany(
        self,
        statement: str,
        parameters: Iterable[Iterable[Any]],
    ) -> PostgresCursor:
        sql, replacement_parameters = translate_sql(statement)
        if replacement_parameters is not None:
            raise ValueError("translated statement cannot be used with executemany")
        cursor = self._connection.cursor()
        cursor.executemany(sql, [tuple(row) for row in parameters])
        return PostgresCursor(cursor)

    def executescript(self, _script: str) -> None:
        # PostgreSQL runtime DDL is intentionally disabled. The versioned Neon
        # migration owns schema creation; store initializers only verify/use it.
        return None

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


class PostgresRepository(SQLiteRepository):
    """Postgres implementation reusing JobSift's repository domain semantics."""

    def __init__(self, database_url: str) -> None:
        value = database_url.strip()
        if not value:
            raise ValueError("Postgres database URL must not be blank")
        if not value.startswith(("postgresql://", "postgres://")):
            raise ValueError("Postgres database URL must use postgresql://")
        self.database_url = value
        self.path = "<postgres>"
        self.remote_url = "postgresql://remote"
        self.auth_token = ""
        self._turso_sync = None
        self._turso_error = ()

        with self.connect() as connection:
            row = connection.execute(
                "SELECT schema_version FROM jobsift_schema_metadata "
                "WHERE singleton=TRUE"
            ).fetchone()
            if row is None or row["schema_version"] != POSTGRES_SCHEMA_VERSION:
                raise RuntimeError(
                    "Neon schema is not initialized at the expected JobSift version"
                )

            # Preserve the SQLite repository's idempotent legacy backfill.
            rows = connection.execute(
                "SELECT j.payload_json FROM jobs j LEFT JOIN posting_delivery_groups g "
                "ON g.job_id=j.id WHERE g.job_id IS NULL ORDER BY j.id"
            ).fetchall()
            for row in rows:
                self._assign_group(connection, Job.model_validate_json(row[0]))
            connection.execute(
                "INSERT OR IGNORE INTO group_deliveries "
                "SELECT g.group_id,e.client_id,e.destination,e.job_id,e.exported_at "
                "FROM exports e JOIN posting_delivery_groups g ON g.job_id=e.job_id "
                "ORDER BY e.exported_at,e.job_id"
            )

    @contextmanager
    def connect(self) -> Iterator[PostgresConnection]:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as error:
            raise RuntimeError(
                "install job-scout[postgres] for Neon/Postgres persistence"
            ) from error

        raw = None
        try:
            raw = psycopg.connect(
                self.database_url,
                row_factory=dict_row,
                autocommit=False,
            )
            connection = PostgresConnection(raw)
            yield connection
            connection.commit()
        except psycopg.IntegrityError as error:
            if raw is not None:
                raw.rollback()
            raise sqlite3.IntegrityError(str(error)) from error
        except psycopg.Error as error:
            if raw is not None:
                raw.rollback()
            raise sqlite3.DatabaseError(str(error)) from error
        except Exception:
            if raw is not None:
                raw.rollback()
            raise
        finally:
            if raw is not None:
                raw.close()
