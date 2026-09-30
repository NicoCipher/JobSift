from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from job_scout.domain.daily_batch import BatchConflict
from job_scout.domain.delivery import (
    DeliveryCampaign,
    DeliveryDestination,
    SheetDeliveryContract,
)

DESTINATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery_destinations (
  destination_id TEXT PRIMARY KEY,
  client_id TEXT NOT NULL,
  spreadsheet_id TEXT NOT NULL,
  worksheet_id INTEGER NOT NULL CHECK(worksheet_id >= 0),
  worksheet_name TEXT NOT NULL,
  header_row INTEGER NOT NULL CHECK(header_row >= 1),
  headers_json TEXT NOT NULL,
  column_map_json TEXT NOT NULL,
  header_sha256 TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ready','blocked')),
  validated_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(spreadsheet_id, worksheet_id)
);
CREATE INDEX IF NOT EXISTS ix_delivery_destinations_client
  ON delivery_destinations(client_id, destination_id);

CREATE TABLE IF NOT EXISTS delivery_campaigns (
  campaign_id TEXT PRIMARY KEY,
  client_id TEXT NOT NULL,
  destination_id TEXT NOT NULL REFERENCES delivery_destinations(destination_id),
  status TEXT NOT NULL CHECK(status IN ('active','paused','completed')),
  created_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_delivery_campaigns_client
  ON delivery_campaigns(client_id, status, campaign_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_campaign_destination
  ON delivery_campaigns(destination_id) WHERE status='active';
"""


def _now() -> datetime:
    return datetime.now(UTC)


class DeliveryDestinationStore:
    def __init__(self, repository):
        self.repository = repository
        with repository.connect() as connection:
            connection.executescript(DESTINATION_SCHEMA)

    @staticmethod
    def _destination(row) -> DeliveryDestination:
        return DeliveryDestination(
            destination_id=row["destination_id"],
            client_id=row["client_id"],
            spreadsheet_id=row["spreadsheet_id"],
            worksheet_id=row["worksheet_id"],
            worksheet_name=row["worksheet_name"],
            header_row=row["header_row"],
            headers=tuple(json.loads(row["headers_json"])),
            column_map=json.loads(row["column_map_json"]),
            header_sha256=row["header_sha256"],
            status=row["status"],
            validated_at=row["validated_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _campaign(row) -> DeliveryCampaign:
        return DeliveryCampaign(
            campaign_id=row["campaign_id"],
            client_id=row["client_id"],
            destination_id=row["destination_id"],
            status=row["status"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
        )

    def register_destination(
        self,
        *,
        destination_id: str,
        client_id: str,
        spreadsheet_id: str,
        worksheet_id: int,
        worksheet_name: str,
        header_row: int,
        headers: tuple[str, ...],
        column_map: dict[str, int],
        header_sha256: str,
        validated_at: datetime,
    ) -> DeliveryDestination:
        destination_id = destination_id.strip()
        client_id = client_id.strip()
        if not destination_id or not client_id:
            raise BatchConflict("destination and client identifiers are required")
        now = _now().isoformat()
        try:
            with self.repository.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT client_id,spreadsheet_id,worksheet_id "
                    "FROM delivery_destinations WHERE destination_id=?",
                    (destination_id,),
                ).fetchone()
                if existing is not None and existing["client_id"] != client_id:
                    raise BatchConflict("destination identifier belongs to another client")
                if (
                    existing is not None
                    and (
                        existing["spreadsheet_id"] != spreadsheet_id
                        or existing["worksheet_id"] != worksheet_id
                    )
                    and connection.execute(
                        "SELECT 1 FROM delivery_campaigns "
                        "WHERE destination_id=? AND status='active' LIMIT 1",
                        (destination_id,),
                    ).fetchone()
                    is not None
                ):
                    raise BatchConflict(
                        "pause or complete the active campaign before moving its destination"
                    )
                connection.execute(
                    "INSERT INTO delivery_destinations "
                    "(destination_id,client_id,spreadsheet_id,worksheet_id,worksheet_name,"
                    "header_row,headers_json,column_map_json,header_sha256,status,validated_at,"
                    "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,'ready',?,?,?) "
                    "ON CONFLICT(destination_id) DO UPDATE SET "
                    "spreadsheet_id=excluded.spreadsheet_id,"
                    "worksheet_id=excluded.worksheet_id,"
                    "worksheet_name=excluded.worksheet_name,"
                    "header_row=excluded.header_row,"
                    "headers_json=excluded.headers_json,"
                    "column_map_json=excluded.column_map_json,"
                    "header_sha256=excluded.header_sha256,"
                    "status='ready',validated_at=excluded.validated_at,"
                    "updated_at=excluded.updated_at",
                    (
                        destination_id,
                        client_id,
                        spreadsheet_id,
                        worksheet_id,
                        worksheet_name,
                        header_row,
                        json.dumps(list(headers)),
                        json.dumps(column_map, sort_keys=True),
                        header_sha256,
                        validated_at.astimezone(UTC).isoformat(),
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM delivery_destinations WHERE destination_id=?",
                    (destination_id,),
                ).fetchone()
        except sqlite3.IntegrityError as error:
            raise BatchConflict(
                "this Google worksheet is already registered to another destination"
            ) from error
        return self._destination(row)

    def get_destination(self, *, client_id: str, destination_id: str) -> DeliveryDestination:
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT * FROM delivery_destinations WHERE destination_id=? AND client_id=?",
                (destination_id, client_id),
            ).fetchone()
        if row is None:
            raise BatchConflict("delivery destination not found for client")
        return self._destination(row)

    def set_destination_status(
        self, *, client_id: str, destination_id: str, status: str
    ) -> DeliveryDestination:
        if status not in {"ready", "blocked"}:
            raise ValueError("invalid destination status")
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE delivery_destinations SET status=?,updated_at=? "
                "WHERE destination_id=? AND client_id=?",
                (status, _now().isoformat(), destination_id, client_id),
            ).rowcount
            if not updated:
                raise BatchConflict("delivery destination not found for client")
            row = connection.execute(
                "SELECT * FROM delivery_destinations WHERE destination_id=?",
                (destination_id,),
            ).fetchone()
        return self._destination(row)

    def bind_campaign(
        self,
        *,
        campaign_id: str,
        client_id: str,
        destination_id: str,
    ) -> DeliveryCampaign:
        campaign_id = campaign_id.strip()
        client_id = client_id.strip()
        destination = self.get_destination(client_id=client_id, destination_id=destination_id)
        if destination.status != "ready":
            raise BatchConflict("cannot bind campaign to a blocked destination")
        now = _now().isoformat()
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM delivery_campaigns WHERE campaign_id=?", (campaign_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["client_id"] != client_id
                    or existing["destination_id"] != destination_id
                ):
                    raise BatchConflict("campaign identifier already has a different binding")
                return self._campaign(existing)
            try:
                connection.execute(
                    "INSERT INTO delivery_campaigns "
                    "(campaign_id,client_id,destination_id,status,created_at,completed_at) "
                    "VALUES (?,?,?,'active',?,NULL)",
                    (campaign_id, client_id, destination_id, now),
                )
            except sqlite3.IntegrityError as error:
                raise BatchConflict(
                    "destination already has another active campaign"
                ) from error
            row = connection.execute(
                "SELECT * FROM delivery_campaigns WHERE campaign_id=?", (campaign_id,)
            ).fetchone()
        return self._campaign(row)

    def set_campaign_status(
        self, *, client_id: str, campaign_id: str, status: str
    ) -> DeliveryCampaign:
        if status not in {"active", "paused", "completed"}:
            raise ValueError("invalid campaign status")
        completed_at = _now().isoformat() if status == "completed" else None
        try:
            with self.repository.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                updated = connection.execute(
                    "UPDATE delivery_campaigns SET status=?,completed_at=? "
                    "WHERE campaign_id=? AND client_id=?",
                    (status, completed_at, campaign_id, client_id),
                ).rowcount
                if not updated:
                    raise BatchConflict("delivery campaign not found for client")
                row = connection.execute(
                    "SELECT * FROM delivery_campaigns WHERE campaign_id=?", (campaign_id,)
                ).fetchone()
        except sqlite3.IntegrityError as error:
            raise BatchConflict(
                "destination already has another active campaign"
            ) from error
        return self._campaign(row)

    def resolve_campaign(self, *, client_id: str, campaign_id: str) -> SheetDeliveryContract:
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT c.campaign_id,c.client_id,c.status AS campaign_status,"
                "d.destination_id,d.spreadsheet_id,d.worksheet_id,d.worksheet_name,"
                "d.header_row,d.headers_json,d.column_map_json,d.header_sha256,"
                "d.status AS destination_status,d.validated_at "
                "FROM delivery_campaigns c JOIN delivery_destinations d "
                "ON d.destination_id=c.destination_id "
                "WHERE c.campaign_id=? AND c.client_id=?",
                (campaign_id, client_id),
            ).fetchone()
        if row is None:
            raise BatchConflict("delivery campaign not found for client")
        if row["campaign_status"] != "active":
            raise BatchConflict("delivery campaign is not active")
        if row["destination_status"] != "ready":
            raise BatchConflict("delivery destination is blocked")
        return SheetDeliveryContract(
            destination_id=row["destination_id"],
            client_id=row["client_id"],
            campaign_id=row["campaign_id"],
            spreadsheet_id=row["spreadsheet_id"],
            worksheet_id=row["worksheet_id"],
            worksheet_name=row["worksheet_name"],
            header_row=row["header_row"],
            headers=tuple(json.loads(row["headers_json"])),
            column_map=json.loads(row["column_map_json"]),
            header_sha256=row["header_sha256"],
            validated_at=row["validated_at"],
        )
