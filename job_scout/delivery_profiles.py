"""Persistent operator-controlled delivery profiles for each client."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from job_scout.delivery_destinations import ClientSheetDestinationStore
from job_scout.search_brief import load_search_brief
from job_scout.storage.sqlite import SQLiteRepository

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BRIEF = re.compile(r"^config/search_briefs/[A-Za-z0-9._/-]+\.json$")

PROFILE_SCHEMA = """
CREATE TABLE IF NOT EXISTS client_delivery_profiles (
  client_id TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  destination_id TEXT NOT NULL,
  brief_path TEXT NOT NULL,
  link_quota INTEGER NOT NULL CHECK(link_quota >= 1 AND link_quota <= 5000),
  quota_scope TEXT NOT NULL CHECK(quota_scope IN ('sheet_total','daily')),
  delivery_mode TEXT NOT NULL CHECK(delivery_mode IN ('review','auto')),
  status TEXT NOT NULL CHECK(status IN ('active','paused')),
  timezone TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(client_id, profile_id),
  UNIQUE(client_id, destination_id)
);
CREATE INDEX IF NOT EXISTS ix_client_delivery_profiles_status
  ON client_delivery_profiles(status, client_id, profile_id);
"""


class ClientDeliveryProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    client_id: str
    profile_id: str
    destination_id: str
    brief_path: str
    link_quota: int = Field(ge=1, le=5000)
    quota_scope: Literal["sheet_total", "daily"] = "sheet_total"
    delivery_mode: Literal["review", "auto"] = "review"
    status: Literal["active", "paused"] = "active"
    timezone: str = "Africa/Lagos"
    created_at: datetime
    updated_at: datetime

    @field_validator("client_id", "profile_id", "destination_id")
    @classmethod
    def stable_id(cls, value: str) -> str:
        value = value.strip()
        if not _ID.fullmatch(value):
            raise ValueError("profile identifiers must be path-safe")
        return value

    @field_validator("brief_path")
    @classmethod
    def safe_brief_path(cls, value: str) -> str:
        value = value.strip()
        if ".." in value or not _BRIEF.fullmatch(value):
            raise ValueError("brief_path must point inside config/search_briefs")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        value = value.strip()
        ZoneInfo(value)
        return value


class ClientDeliveryProfileStore:
    def __init__(self, repository: SQLiteRepository):
        self.repository = repository
        with repository.connect() as connection:
            connection.executescript(PROFILE_SCHEMA)

    @staticmethod
    def _from_row(row) -> ClientDeliveryProfile:
        return ClientDeliveryProfile(
            client_id=row["client_id"],
            profile_id=row["profile_id"],
            destination_id=row["destination_id"],
            brief_path=row["brief_path"],
            link_quota=row["link_quota"],
            quota_scope=row["quota_scope"],
            delivery_mode=row["delivery_mode"],
            status=row["status"],
            timezone=row["timezone"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get(self, client_id: str, profile_id: str) -> ClientDeliveryProfile:
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT * FROM client_delivery_profiles WHERE client_id=? AND profile_id=?",
                (client_id, profile_id),
            ).fetchone()
        if row is None:
            raise ValueError("client delivery profile not found")
        return self._from_row(row)

    def list(self, client_id: str | None = None) -> tuple[ClientDeliveryProfile, ...]:
        with self.repository.connect() as connection:
            if client_id is None:
                rows = connection.execute(
                    "SELECT * FROM client_delivery_profiles ORDER BY client_id,profile_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM client_delivery_profiles WHERE client_id=? "
                    "ORDER BY profile_id",
                    (client_id,),
                ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def active(self) -> tuple[ClientDeliveryProfile, ...]:
        with self.repository.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM client_delivery_profiles WHERE status='active' "
                "ORDER BY client_id,profile_id"
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def configure(
        self,
        *,
        client_id: str,
        profile_id: str,
        destination_id: str,
        brief_path: str,
        link_quota: int,
        quota_scope: Literal["sheet_total", "daily"],
        delivery_mode: Literal["review", "auto"],
        status: Literal["active", "paused"],
        timezone: str,
    ) -> ClientDeliveryProfile:
        now = datetime.now(UTC)
        candidate = ClientDeliveryProfile(
            client_id=client_id,
            profile_id=profile_id,
            destination_id=destination_id,
            brief_path=brief_path,
            link_quota=link_quota,
            quota_scope=quota_scope,
            delivery_mode=delivery_mode,
            status=status,
            timezone=timezone,
            created_at=now,
            updated_at=now,
        )
        ClientSheetDestinationStore(self.repository).get(
            candidate.client_id, candidate.destination_id
        )
        try:
            previous = self.get(candidate.client_id, candidate.profile_id)
        except ValueError:
            previous = None
        candidate = candidate.model_copy(
            update={"created_at": previous.created_at if previous else now}
        )
        try:
            with self.repository.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO client_delivery_profiles "
                    "(client_id,profile_id,destination_id,brief_path,link_quota,quota_scope,"
                    "delivery_mode,"
                    "status,timezone,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(client_id,profile_id) DO UPDATE SET "
                    "destination_id=excluded.destination_id,brief_path=excluded.brief_path,"
                    "link_quota=excluded.link_quota,quota_scope=excluded.quota_scope,"
                    "delivery_mode=excluded.delivery_mode,status=excluded.status,"
                    "timezone=excluded.timezone,updated_at=excluded.updated_at",
                    (
                        candidate.client_id,
                        candidate.profile_id,
                        candidate.destination_id,
                        candidate.brief_path,
                        candidate.link_quota,
                        candidate.quota_scope,
                        candidate.delivery_mode,
                        candidate.status,
                        candidate.timezone,
                        candidate.created_at.isoformat(),
                        candidate.updated_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError(
                "that client Sheet destination is already assigned to another delivery profile"
            ) from error
        return self.get(candidate.client_id, candidate.profile_id)

    def set_status(
        self, client_id: str, profile_id: str, status: Literal["active", "paused"]
    ) -> ClientDeliveryProfile:
        self.get(client_id, profile_id)
        now = datetime.now(UTC)
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE client_delivery_profiles SET status=?,updated_at=? "
                "WHERE client_id=? AND profile_id=?",
                (status, now.isoformat(), client_id, profile_id),
            )
        return self.get(client_id, profile_id)


def _public(value: ClientDeliveryProfile) -> dict[str, object]:
    return value.model_dump(mode="json")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m job_scout.delivery_profiles")
    parser.add_argument("--database", default="jobs.sqlite3")
    commands = parser.add_subparsers(dest="command", required=True)

    configure = commands.add_parser("configure")
    configure.add_argument("--client", required=True)
    configure.add_argument("--profile", default="primary")
    configure.add_argument("--destination", required=True)
    configure.add_argument("--brief", required=True)
    configure.add_argument("--quota", required=True, type=int)
    configure.add_argument("--quota-scope", choices=("sheet_total", "daily"), default="sheet_total")
    configure.add_argument("--mode", choices=("review", "auto"), default="review")
    configure.add_argument("--status", choices=("active", "paused"), default="active")
    configure.add_argument("--timezone", default="Africa/Lagos")

    listing = commands.add_parser("list")
    listing.add_argument("--client")

    for name in ("pause", "resume"):
        command = commands.add_parser(name)
        command.add_argument("--client", required=True)
        command.add_argument("--profile", default="primary")

    args = parser.parse_args()
    repository = SQLiteRepository(args.database)
    store = ClientDeliveryProfileStore(repository)

    if args.command == "configure":
        brief_path = Path(args.brief)
        if not brief_path.is_file():
            parser.error("delivery profile SearchBrief does not exist")
        brief = load_search_brief(brief_path)
        if brief.client_id != args.client:
            parser.error("delivery profile client does not match the SearchBrief")
        try:
            value = store.configure(
                client_id=args.client,
                profile_id=args.profile,
                destination_id=args.destination,
                brief_path=args.brief,
                link_quota=args.quota,
                quota_scope=args.quota_scope,
                delivery_mode=args.mode,
                status=args.status,
                timezone=args.timezone,
            )
        except (OSError, ValueError, sqlite3.Error) as error:
            parser.error(f"unable to configure delivery profile: {error}")
        print(json.dumps(_public(value), sort_keys=True))
        return

    if args.command == "list":
        print(json.dumps([_public(v) for v in store.list(args.client)], sort_keys=True))
        return

    status = "paused" if args.command == "pause" else "active"
    try:
        value = store.set_status(args.client, args.profile, status)
    except (ValueError, sqlite3.Error) as error:
        parser.error(f"unable to update delivery profile: {error}")
    print(json.dumps(_public(value), sort_keys=True))


if __name__ == "__main__":
    main()
