"""PostgreSQL persistence adapter for JobSift production.

The application stores timestamps and JSON as text today so Postgres can preserve
the existing deterministic behavior during migration. SQLite remains the local
and test backend. Production selects this repository explicitly through the
storage factory when NEON_DATABASE_URL is configured.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from job_scout.dedupe.resolver import delivery_keys
from job_scout.domain.models import Job, JobLifecycle
from job_scout.storage.sqlite import SQLiteRepository

POSTGRES_SCHEMA_VERSION = "postgres-v1"
_WRITE_LOCK_KEY = 1246700874
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_POSTGRES_BATCH_ROWS = 500


def _chunks(values, size: int = _POSTGRES_BATCH_ROWS):
    for start in range(0, len(values), size):
        yield values[start : start + size]


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
            "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=%s",
            (table,),
        )
    if re.fullmatch(
        r"SELECT 1 FROM sqlite_master WHERE type='table' AND name=\?",
        normalized,
        flags=re.IGNORECASE,
    ):
        return (
            "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=%s",
            None,
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
    if "?" in sql:
        # psycopg interprets percent signs whenever parameters are supplied.
        # Preserve literal SQL percent signs (for example LIKE 'prefix:%')
        # while converting SQLite qmark placeholders to psycopg placeholders.
        sql = sql.replace("%", "%%").replace("?", "%s")
    return sql, None


class PostgresConnection:
    """Small DB-API compatibility surface used by existing JobSift stores."""

    is_postgres = True

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
        if values:
            cursor.execute(sql, values)
        else:
            cursor.execute(sql)
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
            trigger_count = connection.execute(
                "SELECT COUNT(*) AS count "
                "FROM pg_trigger t "
                "JOIN pg_class c ON c.oid=t.tgrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "JOIN (VALUES "
                "('outcome_events_no_update','operator_outcome_events'),"
                "('outcome_events_no_delete','operator_outcome_events'),"
                "('outcome_events_no_truncate','operator_outcome_events'),"
                "('outcome_imports_no_update','operator_outcome_imports'),"
                "('outcome_imports_no_delete','operator_outcome_imports'),"
                "('outcome_imports_no_truncate','operator_outcome_imports')"
                ") AS expected(trigger_name,table_name) "
                "ON expected.trigger_name=t.tgname AND expected.table_name=c.relname "
                "WHERE NOT t.tgisinternal AND n.nspname='public' "
                "AND t.tgenabled IN ('O','A')"
            ).fetchone()
            if trigger_count is None or int(trigger_count["count"]) != 6:
                raise RuntimeError(
                    "Neon operator-state immutability triggers are not installed"
                )

            # Startup may repair legacy jobs lacking delivery groups. Serialize
            # that repair with every other JobSift writer before inspecting rows.
            connection.execute("BEGIN IMMEDIATE")

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

    def _existing_jobs_in_connection(
        self,
        connection: PostgresConnection,
        jobs: list[Job],
    ) -> dict[tuple[str, str, str], PostgresRow]:
        """Fetch existing provider identities in bounded set-oriented reads."""
        identities = list(
            dict.fromkeys(
                (job.source, job.source_board_id, job.source_job_id)
                for job in jobs
            )
        )
        existing: dict[tuple[str, str, str], PostgresRow] = {}
        for chunk in _chunks(identities):
            placeholders = ",".join("(?,?,?)" for _ in chunk)
            rows = connection.execute(
                "SELECT id,source,source_board_id,source_job_id,content_fingerprint "
                "FROM jobs WHERE (source,source_board_id,source_job_id) IN "
                f"({placeholders})",
                [value for identity in chunk for value in identity],
            ).fetchall()
            for row in rows:
                existing[
                    (row["source"], row["source_board_id"], row["source_job_id"])
                ] = row
        return existing

    def _assign_groups_bulk_in_connection(
        self,
        connection: PostgresConnection,
        jobs: list[Job],
    ) -> None:
        """Assign delivery groups with legacy ordering and bounded DB round trips.

        The row-at-a-time resolver intentionally makes delivery-key visibility
        change as each posting is processed. Preserve that exact behavior in
        memory: a changed posting's old keys disappear before the next posting
        is resolved, while group merges remain sticky forever.
        """
        if not jobs:
            return

        ordered_jobs = list(jobs)
        jobs_by_id = {job.id: job for job in ordered_jobs}
        if len(jobs_by_id) != len(ordered_jobs):
            raise ValueError("bulk group assignment requires unique job IDs")
        job_ids = sorted(jobs_by_id)
        new_keys_by_job = {
            job.id: sorted(delivery_keys(job))
            for job in ordered_jobs
        }

        existing_job_groups: dict[str, str] = {}
        old_keys_by_job: dict[str, set[tuple[str, str]]] = {
            job_id: set() for job_id in job_ids
        }
        for chunk in _chunks(job_ids):
            placeholders = ",".join("?" for _ in chunk)
            group_rows = connection.execute(
                "SELECT job_id,group_id FROM posting_delivery_groups "
                f"WHERE job_id IN ({placeholders})",
                chunk,
            ).fetchall()
            existing_job_groups.update(
                {row["job_id"]: row["group_id"] for row in group_rows}
            )
            key_rows = connection.execute(
                "SELECT job_id,kind,value FROM delivery_keys "
                f"WHERE job_id IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in key_rows:
                old_keys_by_job[row["job_id"]].add(
                    (row["kind"], row["value"])
                )

        relevant_keys = sorted(
            {
                key
                for keys in new_keys_by_job.values()
                for key in keys
            }
        )
        holders_by_key: dict[
            tuple[str, str], dict[str, str]
        ] = {key: {} for key in relevant_keys}
        for chunk in _chunks(relevant_keys):
            placeholders = ",".join("(?,?)" for _ in chunk)
            rows = connection.execute(
                "SELECT k.kind,k.value,k.job_id,g.group_id "
                "FROM delivery_keys k JOIN posting_delivery_groups g "
                "ON g.job_id=k.job_id "
                f"WHERE (k.kind,k.value) IN ({placeholders})",
                [value for key in chunk for value in key],
            ).fetchall()
            for row in rows:
                holders_by_key.setdefault(
                    (row["kind"], row["value"]), {}
                )[row["job_id"]] = row["group_id"]

        parent: dict[str, str] = {}

        def find(value: str) -> str:
            parent.setdefault(value, value)
            while parent[value] != value:
                parent[value] = parent[parent[value]]
                value = parent[value]
            return value

        def union(left: str, right: str) -> str:
            left_root, right_root = find(left), find(right)
            if left_root == right_root:
                return left_root
            winner, loser = sorted((left_root, right_root))
            parent[loser] = winner
            return winner

        database_groups: set[str] = set(existing_job_groups.values())
        for holders in holders_by_key.values():
            database_groups.update(holders.values())
        for group_id in database_groups:
            find(group_id)

        assigned_group_by_job: dict[str, str] = {}
        for job in ordered_jobs:
            own_group = str(
                uuid5(
                    NAMESPACE_URL,
                    json.dumps([job.source, job.source_board_id, job.source_job_id]),
                )
            )
            visible_groups = {find(own_group)}
            existing_group = existing_job_groups.get(job.id)
            if existing_group is not None:
                visible_groups.add(find(existing_group))
            for key in new_keys_by_job[job.id]:
                visible_groups.update(
                    find(group_id)
                    for group_id in holders_by_key.get(key, {}).values()
                )

            group_id = min(visible_groups)
            for other in sorted(visible_groups):
                group_id = union(group_id, other)
            assigned_group_by_job[job.id] = find(group_id)

            # This mirrors legacy _assign_group ordering: resolve against the
            # current key index, then remove this posting's previous evidence,
            # then expose its newly computed evidence to later postings.
            for key in old_keys_by_job[job.id]:
                holders = holders_by_key.get(key)
                if holders is not None:
                    holders.pop(job.id, None)
            for key in new_keys_by_job[job.id]:
                holders_by_key.setdefault(key, {})[job.id] = assigned_group_by_job[
                    job.id
                ]

        final_job_groups = {
            job_id: find(group_id)
            for job_id, group_id in assigned_group_by_job.items()
        }
        canonical_groups = sorted(set(final_job_groups.values()))
        if canonical_groups:
            connection.executemany(
                "INSERT INTO delivery_groups (id) VALUES (?) ON CONFLICT DO NOTHING",
                [(group_id,) for group_id in canonical_groups],
            )

        merge_map = {
            group_id: find(group_id)
            for group_id in database_groups
            if find(group_id) != group_id
        }
        for old_group, new_group in sorted(merge_map.items()):
            connection.execute(
                "INSERT INTO group_deliveries "
                "(group_id,client_id,destination,job_id,exported_at) "
                "SELECT ?,client_id,destination,job_id,exported_at "
                "FROM group_deliveries WHERE group_id=? "
                "ON CONFLICT DO NOTHING",
                (new_group, old_group),
            )
            connection.execute(
                "DELETE FROM group_deliveries WHERE group_id=?",
                (old_group,),
            )
            connection.execute(
                "UPDATE posting_delivery_groups SET group_id=? WHERE group_id=?",
                (new_group, old_group),
            )
            connection.execute(
                "DELETE FROM delivery_groups WHERE id=?",
                (old_group,),
            )

        connection.executemany(
            "INSERT INTO posting_delivery_groups (job_id,group_id) VALUES (?,?) "
            "ON CONFLICT(job_id) DO UPDATE SET group_id=excluded.group_id",
            [(job_id, final_job_groups[job_id]) for job_id in job_ids],
        )
        for chunk in _chunks(job_ids):
            placeholders = ",".join("?" for _ in chunk)
            connection.execute(
                f"DELETE FROM delivery_keys WHERE job_id IN ({placeholders})",
                chunk,
            )
        key_rows = [
            (job.id, kind, value)
            for job in ordered_jobs
            for kind, value in new_keys_by_job[job.id]
        ]
        if key_rows:
            connection.executemany(
                "INSERT INTO delivery_keys (job_id,kind,value) VALUES (?,?,?) "
                "ON CONFLICT DO NOTHING",
                key_rows,
            )

    def _upsert_jobs_in_connection(
        self,
        connection: PostgresConnection,
        jobs: Iterable[Job],
        now: str,
    ) -> dict[str, JobLifecycle]:
        """Set-oriented Postgres upsert sized for 20k-posting fan-in payloads."""
        values = list(jobs)
        if not values:
            return {}

        known = self._existing_jobs_in_connection(connection, values)
        states: dict[str, JobLifecycle] = {}
        rows = []
        for job in values:
            identity = (job.source, job.source_board_id, job.source_job_id)
            previous = known.get(identity)
            if previous is None:
                state = JobLifecycle.NEW
                known[identity] = {
                    "id": job.id,
                    "content_fingerprint": job.content_fingerprint,
                }
            else:
                job.id = previous["id"]
                state = (
                    JobLifecycle.CHANGED
                    if previous["content_fingerprint"] != job.content_fingerprint
                    else JobLifecycle.SEEN
                )
                known[identity] = {
                    "id": job.id,
                    "content_fingerprint": job.content_fingerprint,
                }
            states[job.id] = state
            rows.append(
                (
                    job.id,
                    job.source,
                    job.source_job_id,
                    job.source_board_id,
                    str(job.canonical_url),
                    job.content_fingerprint,
                    job.discovered_at.isoformat(),
                    job.last_seen_at.isoformat(),
                    now,
                    state.value,
                    job.model_dump_json(),
                )
            )

        connection.executemany(
            "INSERT INTO jobs "
            "(id,source,source_job_id,source_board_id,canonical_url,"
            "content_fingerprint,first_seen_at,last_seen_at,last_verified_at,"
            "lifecycle,payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source,source_board_id,source_job_id) DO UPDATE SET "
            "canonical_url=excluded.canonical_url,"
            "content_fingerprint=excluded.content_fingerprint,"
            "last_seen_at=excluded.last_seen_at,"
            "last_verified_at=excluded.last_verified_at,"
            "lifecycle=excluded.lifecycle,"
            "payload_json=excluded.payload_json",
            rows,
        )

        job_ids = sorted({job.id for job in values})
        for chunk in _chunks(job_ids):
            placeholders = ",".join("?" for _ in chunk)
            connection.execute(
                f"DELETE FROM job_retention_evidence WHERE job_id IN ({placeholders})",
                chunk,
            )

        self._assign_groups_bulk_in_connection(connection, values)
        return states

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
                connect_timeout=5,
            )
            raw.execute("SET LOCAL lock_timeout = '5s'")
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
