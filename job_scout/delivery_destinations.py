"""Persistent client-owned delivery destinations and Google Sheet onboarding."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Literal, Protocol
from urllib.parse import parse_qs, quote, unquote, urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from job_scout.domain.daily_batch import BatchConflict
from job_scout.export.csv_exporter import CSV_COLUMNS

DESTINATION_SCHEME = "client-sheet"
SYSTEM_FIELDS = ("Status", "Batch ID", "Batch Prepared At", "Job ID")
DELIVERY_FIELDS = (*CSV_COLUMNS, *SYSTEM_FIELDS)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

DESTINATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS client_sheet_destinations (
  client_id TEXT NOT NULL,
  destination_id TEXT NOT NULL,
  display_name TEXT NOT NULL,
  spreadsheet_id TEXT NOT NULL,
  sheet_id INTEGER NOT NULL,
  tab_name TEXT NOT NULL,
  header_json TEXT NOT NULL,
  header_sha256 TEXT NOT NULL,
  column_mapping_json TEXT NOT NULL,
  config_sha256 TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ready','disabled')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(client_id, destination_id)
);
CREATE INDEX IF NOT EXISTS ix_client_sheet_destinations_client
  ON client_sheet_destinations(client_id, status, destination_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_client_sheet_physical_worksheet
  ON client_sheet_destinations(spreadsheet_id, sheet_id);
"""


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def logical_destination(destination_id: str) -> str:
    if not _ID.fullmatch(destination_id):
        raise BatchConflict("invalid client destination ID")
    return f"{DESTINATION_SCHEME}://{quote(destination_id, safe='._-')}"


def parse_logical_destination(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != DESTINATION_SCHEME or parsed.path or parsed.query or parsed.fragment:
        raise BatchConflict("invalid client Sheet destination")
    destination_id = unquote(parsed.netloc)
    if value != logical_destination(destination_id):
        raise BatchConflict("client Sheet destination is not canonical")
    return destination_id


def spreadsheet_id_from_value(value: str) -> str:
    value = value.strip()
    if _ID.fullmatch(value):
        return value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "docs.google.com",
        "drive.google.com",
    }:
        raise ValueError("expected a Google Sheets URL or spreadsheet ID")
    parts = [part for part in parsed.path.split("/") if part]
    if "d" in parts:
        index = parts.index("d")
        if index + 1 < len(parts) and _ID.fullmatch(parts[index + 1]):
            return parts[index + 1]
    query_id = parse_qs(parsed.query).get("id", [])
    if query_id and _ID.fullmatch(query_id[0]):
        return query_id[0]
    raise ValueError("Google Sheets URL does not contain a valid spreadsheet ID")


class ClientSheetRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    client_id: str
    destination_id: str
    display_name: str
    spreadsheet: str
    tab: str
    column_mapping: dict[str, str]

    @field_validator("client_id", "display_name", "spreadsheet", "tab")
    @classmethod
    def required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("registration fields must not be blank")
        return value

    @field_validator("destination_id")
    @classmethod
    def stable_destination_id(cls, value: str) -> str:
        value = value.strip()
        if not _ID.fullmatch(value):
            raise ValueError("destination_id must be path-safe")
        return value

    @field_validator("column_mapping")
    @classmethod
    def valid_mapping(cls, value: dict[str, str]) -> dict[str, str]:
        cleaned = {
            str(source).strip(): str(target).strip()
            for source, target in value.items()
        }
        if not cleaned or any(not source or not target for source, target in cleaned.items()):
            raise ValueError("column_mapping must contain nonblank field/header pairs")
        return cleaned


class SheetMetadataGateway(Protocol):
    def read_rows(self, spreadsheet_id: str, tab: str) -> list[list[str]]: ...

    def sheet_metadata(self, spreadsheet_id: str) -> list[dict[str, object]]: ...


class ClientSheetDestination(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    client_id: str
    destination_id: str
    display_name: str
    spreadsheet_id: str
    sheet_id: int = Field(ge=0)
    tab_name: str
    header: tuple[str, ...]
    header_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    column_mapping: dict[str, str]
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["ready", "disabled"] = "ready"
    created_at: datetime
    updated_at: datetime

    @field_validator("client_id", "display_name", "tab_name")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("destination_id")
    @classmethod
    def destination_id_is_stable(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("destination_id must be path-safe")
        return value

    @field_validator("spreadsheet_id")
    @classmethod
    def spreadsheet_id_is_valid(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("invalid spreadsheet ID")
        return value

    @model_validator(mode="after")
    def mapping_is_safe(self) -> ClientSheetDestination:
        if not self.header:
            raise ValueError("sheet must have a header row")
        unknown = set(self.column_mapping) - set(DELIVERY_FIELDS)
        if unknown:
            raise ValueError(f"unsupported delivery fields: {', '.join(sorted(unknown))}")
        if "Job Link" not in self.column_mapping:
            raise ValueError("column mapping must include Job Link")
        targets = list(self.column_mapping.values())
        if len(set(targets)) != len(targets):
            raise ValueError("each client column can be mapped only once")
        for target in targets:
            if self.header.count(target) != 1:
                raise ValueError(
                    f"mapped client header must exist exactly once: {target!r}"
                )
        if self.header_sha256 != _sha(list(self.header)):
            raise ValueError("header fingerprint does not match header")
        expected = destination_config_sha(
            client_id=self.client_id,
            destination_id=self.destination_id,
            spreadsheet_id=self.spreadsheet_id,
            sheet_id=self.sheet_id,
            tab_name=self.tab_name,
            header=self.header,
            column_mapping=self.column_mapping,
        )
        if self.config_sha256 != expected:
            raise ValueError("destination config fingerprint does not match")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("destination timestamps require timezone")
        return self

    @property
    def logical_uri(self) -> str:
        return logical_destination(self.destination_id)


def destination_config_sha(
    *,
    client_id: str,
    destination_id: str,
    spreadsheet_id: str,
    sheet_id: int,
    tab_name: str,
    header: tuple[str, ...],
    column_mapping: dict[str, str],
) -> str:
    return _sha(
        {
            "client_id": client_id,
            "destination_id": destination_id,
            "spreadsheet_id": spreadsheet_id,
            "sheet_id": sheet_id,
            "tab_name": tab_name,
            "header": list(header),
            "column_mapping": column_mapping,
        }
    )


class ClientSheetDestinationStore:
    def __init__(self, repository):
        self.repository = repository
        try:
            with repository.connect() as connection:
                connection.executescript(DESTINATION_SCHEMA)
        except sqlite3.IntegrityError as error:
            raise BatchConflict(
                "duplicate physical client worksheets exist; reconcile destination ownership"
            ) from error

    @staticmethod
    def _from_row(row) -> ClientSheetDestination:
        return ClientSheetDestination(
            client_id=row["client_id"],
            destination_id=row["destination_id"],
            display_name=row["display_name"],
            spreadsheet_id=row["spreadsheet_id"],
            sheet_id=row["sheet_id"],
            tab_name=row["tab_name"],
            header=tuple(json.loads(row["header_json"])),
            header_sha256=row["header_sha256"],
            column_mapping=json.loads(row["column_mapping_json"]),
            config_sha256=row["config_sha256"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get(
        self, client_id: str, destination_id: str, *, require_ready: bool = True
    ) -> ClientSheetDestination:
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT * FROM client_sheet_destinations "
                "WHERE client_id=? AND destination_id=?",
                (client_id, destination_id),
            ).fetchone()
        if row is None:
            raise BatchConflict("client delivery destination not found")
        value = self._from_row(row)
        if require_ready and value.status != "ready":
            raise BatchConflict("client delivery destination is disabled")
        return value

    def list(self, client_id: str) -> tuple[ClientSheetDestination, ...]:
        with self.repository.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM client_sheet_destinations WHERE client_id=? "
                "ORDER BY destination_id",
                (client_id,),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def prepare_google_sheet(
        self,
        *,
        client_id: str,
        destination_id: str,
        display_name: str,
        spreadsheet: str,
        tab_name: str,
        column_mapping: dict[str, str],
        gateway: SheetMetadataGateway,
    ) -> ClientSheetDestination:
        """Verify one Google worksheet and build its immutable registration value.

        This method performs no destination write. Callers that need a larger
        atomic mutation can persist the returned value inside their transaction.
        """
        spreadsheet_id = spreadsheet_id_from_value(spreadsheet)
        metadata = gateway.sheet_metadata(spreadsheet_id)
        matches = [
            item for item in metadata if str(item.get("title", "")).strip() == tab_name.strip()
        ]
        if len(matches) != 1:
            raise BatchConflict("Google Sheets tab was not found uniquely")
        sheet_id = matches[0].get("sheet_id")
        if not isinstance(sheet_id, int) or sheet_id < 0:
            raise BatchConflict("Google Sheets metadata did not provide a stable sheet ID")
        rows = gateway.read_rows(spreadsheet_id, tab_name)
        if not rows:
            raise BatchConflict("Google Sheets tab has no header row")
        header = tuple(str(value) for value in rows[0])
        now = datetime.now(UTC)
        header_sha = _sha(list(header))
        config_sha = destination_config_sha(
            client_id=client_id,
            destination_id=destination_id,
            spreadsheet_id=spreadsheet_id,
            sheet_id=sheet_id,
            tab_name=tab_name.strip(),
            header=header,
            column_mapping=column_mapping,
        )
        existing = None
        try:
            existing = self.get(client_id, destination_id, require_ready=False)
        except BatchConflict:
            pass
        return ClientSheetDestination(
            client_id=client_id,
            destination_id=destination_id,
            display_name=display_name,
            spreadsheet_id=spreadsheet_id,
            sheet_id=sheet_id,
            tab_name=tab_name.strip(),
            header=header,
            header_sha256=header_sha,
            column_mapping=column_mapping,
            config_sha256=config_sha,
            status="ready",
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )

    @staticmethod
    def write_prepared(connection, value: ClientSheetDestination) -> None:
        """Persist a verified destination using the caller's transaction."""
        owner = connection.execute(
            "SELECT client_id,destination_id FROM client_sheet_destinations "
            "WHERE spreadsheet_id=? AND sheet_id=? "
            "AND NOT (client_id=? AND destination_id=?)",
            (
                value.spreadsheet_id,
                value.sheet_id,
                value.client_id,
                value.destination_id,
            ),
        ).fetchone()
        if owner is not None:
            raise BatchConflict(
                "Google worksheet is already registered to another JobSift destination"
            )
        legacy_destination = (
            f"gsheet://{value.spreadsheet_id}/"
            + quote(value.tab_name, safe="")
        )
        logical = value.logical_uri
        connection.execute(
            "INSERT OR IGNORE INTO group_deliveries "
            "(group_id,client_id,destination,job_id,exported_at) "
            "SELECT group_id,client_id,?,job_id,exported_at "
            "FROM group_deliveries "
            "WHERE client_id=? AND destination=?",
            (logical, value.client_id, legacy_destination),
        )
        connection.execute(
            "INSERT OR IGNORE INTO exports "
            "(job_id,client_id,destination,exported_at) "
            "SELECT job_id,client_id,?,exported_at "
            "FROM exports WHERE client_id=? AND destination=?",
            (logical, value.client_id, legacy_destination),
        )
        connection.execute(
            "INSERT INTO client_sheet_destinations "
            "(client_id,destination_id,display_name,spreadsheet_id,sheet_id,tab_name,"
            "header_json,header_sha256,column_mapping_json,config_sha256,status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(client_id,destination_id) DO UPDATE SET "
            "display_name=excluded.display_name,"
            "spreadsheet_id=excluded.spreadsheet_id,"
            "sheet_id=excluded.sheet_id,"
            "tab_name=excluded.tab_name,"
            "header_json=excluded.header_json,"
            "header_sha256=excluded.header_sha256,"
            "column_mapping_json=excluded.column_mapping_json,"
            "config_sha256=excluded.config_sha256,"
            "status=excluded.status,"
            "updated_at=excluded.updated_at",
            (
                value.client_id,
                value.destination_id,
                value.display_name,
                value.spreadsheet_id,
                value.sheet_id,
                value.tab_name,
                json.dumps(list(value.header), ensure_ascii=False),
                value.header_sha256,
                json.dumps(value.column_mapping, sort_keys=True, ensure_ascii=False),
                value.config_sha256,
                value.status,
                value.created_at.isoformat(),
                value.updated_at.isoformat(),
            ),
        )

    def register_google_sheet(
        self,
        *,
        client_id: str,
        destination_id: str,
        display_name: str,
        spreadsheet: str,
        tab_name: str,
        column_mapping: dict[str, str],
        gateway: SheetMetadataGateway,
    ) -> ClientSheetDestination:
        value = self.prepare_google_sheet(
            client_id=client_id,
            destination_id=destination_id,
            display_name=display_name,
            spreadsheet=spreadsheet,
            tab_name=tab_name,
            column_mapping=column_mapping,
            gateway=gateway,
        )
        try:
            with self.repository.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self.write_prepared(connection, value)
        except sqlite3.IntegrityError as error:
            raise BatchConflict(
                "Google worksheet is already registered to another JobSift destination"
            ) from error
        return value

    def enable_verified(
        self,
        client_id: str,
        destination_id: str,
        *,
        gateway: SheetMetadataGateway,
    ) -> ClientSheetDestination:
        """Re-enable only the exact registered worksheet after verifying stable identity/schema."""
        current = self.get(client_id, destination_id, require_ready=False)
        metadata = gateway.sheet_metadata(current.spreadsheet_id)
        matches = [
            item
            for item in metadata
            if item.get("sheet_id") == current.sheet_id
        ]
        if len(matches) != 1:
            raise BatchConflict("registered Google Sheet tab no longer exists")
        if matches[0].get("title") != current.tab_name:
            raise BatchConflict(
                "Google Sheet tab was renamed; refresh the destination registration"
            )
        rows = gateway.read_rows(current.spreadsheet_id, current.tab_name)
        if not rows or tuple(str(value) for value in rows[0]) != current.header:
            raise BatchConflict(
                "Google Sheets header differs from the registered client schema"
            )
        now = datetime.now(UTC)
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE client_sheet_destinations SET status='ready',updated_at=? "
                "WHERE client_id=? AND destination_id=?",
                (now.isoformat(), client_id, destination_id),
            )
        return current.model_copy(update={"status": "ready", "updated_at": now})

    def disable(self, client_id: str, destination_id: str) -> ClientSheetDestination:
        current = self.get(client_id, destination_id, require_ready=False)
        now = datetime.now(UTC)
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE client_sheet_destinations SET status='disabled',updated_at=? "
                "WHERE client_id=? AND destination_id=?",
                (now.isoformat(), client_id, destination_id),
            )
        return current.model_copy(update={"status": "disabled", "updated_at": now})
