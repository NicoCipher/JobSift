"""Durable operator-managed clients and private provisioning requests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from job_scout.domain.models import SearchBrief

OPERATOR_CLIENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS operator_clients (
  client_id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  destination_id TEXT NOT NULL,
  destination_name TEXT NOT NULL,
  sourcing_plan_id TEXT NOT NULL UNIQUE,
  search_brief_json TEXT NOT NULL,
  brief_sha256 TEXT NOT NULL,
  brief_revision INTEGER NOT NULL CHECK(brief_revision > 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(client_id, destination_id)
);
CREATE TABLE IF NOT EXISTS operator_provisioning_requests (
  request_id TEXT PRIMARY KEY,
  operation TEXT NOT NULL CHECK(operation IN (
    'inspect_sheet','create_client','update_criteria','update_sheet'
  )),
  state TEXT NOT NULL CHECK(state IN ('queued','running','completed','failed')),
  request_sha256 TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  result_json TEXT,
  error_code TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_operator_provisioning_requests_state
  ON operator_provisioning_requests(state, created_at, request_id);
"""

_POSTGRES_OPERATOR_CLIENT_DDL = (
    """
    CREATE TABLE IF NOT EXISTS operator_clients (
      client_id TEXT PRIMARY KEY,
      display_name TEXT NOT NULL,
      destination_id TEXT NOT NULL,
      destination_name TEXT NOT NULL,
      sourcing_plan_id TEXT NOT NULL UNIQUE,
      search_brief_json TEXT NOT NULL,
      brief_sha256 TEXT NOT NULL,
      brief_revision INTEGER NOT NULL CHECK(brief_revision > 0),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE(client_id, destination_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS operator_provisioning_requests (
      request_id TEXT PRIMARY KEY,
      operation TEXT NOT NULL CHECK(operation IN (
        'inspect_sheet','create_client','update_criteria','update_sheet'
      )),
      state TEXT NOT NULL CHECK(state IN ('queued','running','completed','failed')),
      request_sha256 TEXT NOT NULL,
      payload_json TEXT NOT NULL,
      result_json TEXT,
      error_code TEXT,
      error_message TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_operator_provisioning_requests_state
      ON operator_provisioning_requests(state, created_at, request_id)
    """,
)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def provisioning_payload_sha256(payload: dict[str, object]) -> str:
    return _sha(payload)


def brief_sha256(brief: SearchBrief) -> str:
    return _sha(brief.model_dump(mode="json"))


class OperatorClientConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    client_id: str
    display_name: str
    destination_id: str
    destination_name: str
    sourcing_plan_id: str
    brief: SearchBrief
    brief_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    brief_revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime

    @field_validator(
        "client_id",
        "display_name",
        "destination_id",
        "destination_name",
        "sourcing_plan_id",
    )
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("operator client fields must not be blank")
        return value


class OperatorProvisioningRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str
    operation: Literal[
        "inspect_sheet",
        "create_client",
        "update_criteria",
        "update_sheet",
    ]
    state: Literal["queued", "running", "completed", "failed"]
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: dict[str, object]
    result: dict[str, object] | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("request_id")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        parsed = UUID(value)
        canonical = str(parsed)
        if canonical != value.casefold():
            raise ValueError("request_id must be a canonical UUID")
        return canonical


class OperatorClientStore:
    def __init__(self, repository) -> None:
        self.repository = repository
        if not self._is_postgres():
            with self.repository.connect() as connection:
                connection.executescript(OPERATOR_CLIENT_SCHEMA)

    def _is_postgres(self) -> bool:
        return self.repository.__class__.__name__ == "PostgresRepository"

    def available(self) -> bool:
        with self.repository.connect() as connection:
            if getattr(connection, "is_postgres", False):
                row = connection.execute(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name='operator_clients'"
                    ") AS present"
                ).fetchone()
                return bool(row and row["present"])
            return True

    @staticmethod
    def _from_row(row) -> OperatorClientConfig:
        brief = SearchBrief.model_validate_json(row["search_brief_json"])
        return OperatorClientConfig(
            client_id=row["client_id"],
            display_name=row["display_name"],
            destination_id=row["destination_id"],
            destination_name=row["destination_name"],
            sourcing_plan_id=row["sourcing_plan_id"],
            brief=brief,
            brief_sha256=row["brief_sha256"],
            brief_revision=row["brief_revision"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def get(self, client_id: str) -> OperatorClientConfig | None:
        if not self.available():
            return None
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT * FROM operator_clients WHERE client_id=?",
                (client_id,),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def get_for_profile(
        self,
        client_id: str,
        destination_id: str,
    ) -> OperatorClientConfig | None:
        if not self.available():
            return None
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT * FROM operator_clients "
                "WHERE client_id=? AND destination_id=?",
                (client_id, destination_id),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def list(self) -> tuple[OperatorClientConfig, ...]:
        if not self.available():
            return ()
        with self.repository.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM operator_clients ORDER BY display_name,client_id"
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def upsert(
        self,
        *,
        client_id: str,
        display_name: str,
        destination_id: str,
        destination_name: str,
        sourcing_plan_id: str,
        brief: SearchBrief,
    ) -> OperatorClientConfig:
        if brief.client_id != client_id:
            raise ValueError("SearchBrief client does not match operator client")
        now = datetime.now(UTC)
        existing = self.get(client_id)
        revision = (existing.brief_revision + 1) if existing else 1
        created_at = existing.created_at if existing else now
        digest = brief_sha256(brief)
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO operator_clients "
                "(client_id,display_name,destination_id,destination_name,sourcing_plan_id,"
                "search_brief_json,brief_sha256,brief_revision,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(client_id) DO UPDATE SET "
                "display_name=excluded.display_name,"
                "destination_id=excluded.destination_id,"
                "destination_name=excluded.destination_name,"
                "sourcing_plan_id=excluded.sourcing_plan_id,"
                "search_brief_json=excluded.search_brief_json,"
                "brief_sha256=excluded.brief_sha256,"
                "brief_revision=excluded.brief_revision,"
                "updated_at=excluded.updated_at",
                (
                    client_id,
                    display_name.strip(),
                    destination_id.strip(),
                    destination_name.strip(),
                    sourcing_plan_id.strip(),
                    brief.model_dump_json(),
                    digest,
                    revision,
                    created_at.isoformat(),
                    now.isoformat(),
                ),
            )
        value = self.get(client_id)
        if value is None:
            raise RuntimeError("operator client was not persisted")
        return value


class OperatorProvisioningStore:
    def __init__(self, repository) -> None:
        self.repository = repository
        if self._is_postgres():
            self._ensure_postgres_schema()
        else:
            with self.repository.connect() as connection:
                connection.executescript(OPERATOR_CLIENT_SCHEMA)

    def _ensure_postgres_schema(self) -> None:
        with self.repository.connect() as connection:
            for statement in _POSTGRES_OPERATOR_CLIENT_DDL:
                connection.execute(statement)

    def _is_postgres(self) -> bool:
        return self.repository.__class__.__name__ == "PostgresRepository"

    def available(self) -> bool:
        with self.repository.connect() as connection:
            if getattr(connection, "is_postgres", False):
                row = connection.execute(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema='public' "
                    "AND table_name='operator_provisioning_requests'"
                    ") AS present"
                ).fetchone()
                return bool(row and row["present"])
            return True

    @staticmethod
    def _from_row(row) -> OperatorProvisioningRequest:
        return OperatorProvisioningRequest(
            request_id=row["request_id"],
            operation=row["operation"],
            state=row["state"],
            request_sha256=row["request_sha256"],
            payload=json.loads(row["payload_json"]),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def create(
        self,
        *,
        request_id: str,
        operation: str,
        payload: dict[str, object],
    ) -> OperatorProvisioningRequest:
        UUID(request_id)
        if operation not in {
            "inspect_sheet",
            "create_client",
            "update_criteria",
            "update_sheet",
        }:
            raise ValueError("unsupported provisioning operation")
        now = datetime.now(UTC).isoformat()
        request_sha = provisioning_payload_sha256(payload)
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO operator_provisioning_requests "
                "(request_id,operation,state,request_sha256,payload_json,result_json,"
                "error_code,error_message,created_at,updated_at) "
                "VALUES (?,?, 'queued', ?, ?, NULL, NULL, NULL, ?, ?)",
                (
                    request_id.casefold(),
                    operation,
                    request_sha,
                    _canonical(payload),
                    now,
                    now,
                ),
            )
        value = self.get(request_id)
        if value is None:
            raise RuntimeError("provisioning request was not persisted")
        return value

    def get(self, request_id: str) -> OperatorProvisioningRequest | None:
        if not self.available():
            return None
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT * FROM operator_provisioning_requests WHERE request_id=?",
                (request_id.casefold(),),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def claim(
        self,
        request_id: str,
        *,
        now: datetime | None = None,
    ) -> OperatorProvisioningRequest:
        if not self.available():
            raise RuntimeError("operator provisioning schema is not installed")
        current_time = now or datetime.now(UTC)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=UTC)
        lease_cutoff = current_time - timedelta(minutes=15)
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM operator_provisioning_requests WHERE request_id=?",
                (request_id.casefold(),),
            ).fetchone()
            if row is None:
                raise ValueError("provisioning request not found")
            current = self._from_row(row)
            if current.state == "completed":
                return current
            if current.state == "running" and current.updated_at > lease_cutoff:
                raise ValueError("provisioning request is already running")
            updated = connection.execute(
                "UPDATE operator_provisioning_requests "
                "SET state='running',payload_json='{}',error_code=NULL,error_message=NULL,"
                "updated_at=? WHERE request_id=? AND state=? AND updated_at=?",
                (
                    current_time.isoformat(),
                    request_id.casefold(),
                    current.state,
                    row["updated_at"],
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("provisioning request state changed")
        value = self.get(request_id)
        if value is None:
            raise RuntimeError("claimed provisioning request disappeared")
        return value

    def complete(
        self,
        request_id: str,
        result: dict[str, object],
    ) -> OperatorProvisioningRequest:
        now = datetime.now(UTC).isoformat()
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE operator_provisioning_requests "
                "SET state='completed',payload_json='{}',result_json=?,"
                "error_code=NULL,error_message=NULL,"
                "updated_at=? WHERE request_id=? AND state='running'",
                (_canonical(result), now, request_id.casefold()),
            )
            if updated.rowcount != 1:
                raise ValueError("provisioning request is not running")
        value = self.get(request_id)
        if value is None:
            raise RuntimeError("completed provisioning request disappeared")
        return value

    def fail(
        self,
        request_id: str,
        *,
        code: str,
        message: str,
    ) -> OperatorProvisioningRequest:
        now = datetime.now(UTC).isoformat()
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE operator_provisioning_requests "
                "SET state='failed',payload_json='{}',error_code=?,error_message=?,updated_at=? "
                "WHERE request_id=? AND state='running'",
                (code[:80], message[:1000], now, request_id.casefold()),
            )
        value = self.get(request_id)
        if value is None:
            raise RuntimeError("failed provisioning request disappeared")
        return value
