"""Persistent per-client delivery controls.

A delivery profile binds one registered client-owned destination to the
operator-controlled quota and delivery behaviour. Sourcing remains independent;
profiles decide how much of the fresh shared inventory may be delivered.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from job_scout.delivery_destinations import ClientSheetDestinationStore
from job_scout.domain.daily_batch import BatchConflict

PROFILE_SCHEMA = """
CREATE TABLE IF NOT EXISTS client_delivery_profiles (
  client_id TEXT NOT NULL,
  destination_id TEXT NOT NULL,
  sourcing_plan_id TEXT NOT NULL,
  daily_quota INTEGER NOT NULL CHECK(daily_quota >= 1 AND daily_quota <= 5000),
  status TEXT NOT NULL CHECK(status IN ('active','paused')),
  delivery_mode TEXT NOT NULL CHECK(delivery_mode IN ('review','auto')),
  timezone TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(client_id, destination_id)
);
CREATE INDEX IF NOT EXISTS ix_client_delivery_profiles_status
  ON client_delivery_profiles(status, client_id, destination_id);
"""


class ClientDeliveryProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    client_id: str
    destination_id: str
    sourcing_plan_id: str
    daily_quota: int = Field(ge=1, le=5000)
    status: Literal["active", "paused"] = "paused"
    delivery_mode: Literal["review", "auto"] = "review"
    timezone: str = "Africa/Lagos"
    created_at: datetime
    updated_at: datetime

    @field_validator("client_id", "destination_id", "sourcing_plan_id")
    @classmethod
    def nonblank_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("profile identifiers must not be blank")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("timezone must not be blank")
        ZoneInfo(value)
        return value

    @model_validator(mode="after")
    def aware_timestamps(self) -> ClientDeliveryProfile:
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("profile timestamps require timezone")
        return self


class ClientDeliveryProfileStore:
    def __init__(self, repository):
        self.repository = repository
        with repository.connect() as connection:
            connection.executescript(PROFILE_SCHEMA)

    @staticmethod
    def _from_row(row) -> ClientDeliveryProfile:
        return ClientDeliveryProfile(
            client_id=row["client_id"],
            destination_id=row["destination_id"],
            sourcing_plan_id=row["sourcing_plan_id"],
            daily_quota=row["daily_quota"],
            status=row["status"],
            delivery_mode=row["delivery_mode"],
            timezone=row["timezone"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def get(self, client_id: str, destination_id: str) -> ClientDeliveryProfile:
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT * FROM client_delivery_profiles "
                "WHERE client_id=? AND destination_id=?",
                (client_id, destination_id),
            ).fetchone()
        if row is None:
            raise BatchConflict("client delivery profile not found")
        return self._from_row(row)

    def list(self, client_id: str | None = None) -> tuple[ClientDeliveryProfile, ...]:
        with self.repository.connect() as connection:
            if client_id is None:
                rows = connection.execute(
                    "SELECT * FROM client_delivery_profiles "
                    "ORDER BY client_id,destination_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM client_delivery_profiles WHERE client_id=? "
                    "ORDER BY destination_id",
                    (client_id,),
                ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def active(self) -> tuple[ClientDeliveryProfile, ...]:
        with self.repository.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM client_delivery_profiles WHERE status='active' "
                "ORDER BY client_id,destination_id"
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def upsert(
        self,
        *,
        client_id: str,
        destination_id: str,
        sourcing_plan_id: str,
        daily_quota: int,
        status: Literal["active", "paused"],
        delivery_mode: Literal["review", "auto"],
        timezone: str,
    ) -> ClientDeliveryProfile:
        destination = ClientSheetDestinationStore(self.repository).get(
            client_id, destination_id, require_ready=False
        )
        if status == "active" and destination.status != "ready":
            raise BatchConflict("active profile requires a ready delivery destination")
        ZoneInfo(timezone)

        now = datetime.now(UTC)
        try:
            existing = self.get(client_id, destination_id)
            created_at = existing.created_at
        except BatchConflict:
            created_at = now

        value = ClientDeliveryProfile(
            client_id=client_id,
            destination_id=destination_id,
            sourcing_plan_id=sourcing_plan_id,
            daily_quota=daily_quota,
            status=status,
            delivery_mode=delivery_mode,
            timezone=timezone,
            created_at=created_at,
            updated_at=now,
        )
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO client_delivery_profiles "
                "(client_id,destination_id,sourcing_plan_id,daily_quota,status,"
                "delivery_mode,timezone,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(client_id,destination_id) DO UPDATE SET "
                "sourcing_plan_id=excluded.sourcing_plan_id,"
                "daily_quota=excluded.daily_quota,"
                "status=excluded.status,"
                "delivery_mode=excluded.delivery_mode,"
                "timezone=excluded.timezone,"
                "updated_at=excluded.updated_at",
                (
                    value.client_id,
                    value.destination_id,
                    value.sourcing_plan_id,
                    value.daily_quota,
                    value.status,
                    value.delivery_mode,
                    value.timezone,
                    value.created_at.isoformat(),
                    value.updated_at.isoformat(),
                ),
            )
        return value

    def set_status(
        self,
        client_id: str,
        destination_id: str,
        status: Literal["active", "paused"],
    ) -> ClientDeliveryProfile:
        current = self.get(client_id, destination_id)
        return self.upsert(
            client_id=current.client_id,
            destination_id=current.destination_id,
            sourcing_plan_id=current.sourcing_plan_id,
            daily_quota=current.daily_quota,
            status=status,
            delivery_mode=current.delivery_mode,
            timezone=current.timezone,
        )

    @staticmethod
    def _day_window(profile: ClientDeliveryProfile, now: datetime | None = None) -> tuple[datetime, datetime]:
        zone = ZoneInfo(profile.timezone)
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        local = current.astimezone(zone)
        start_local = datetime(local.year, local.month, local.day, tzinfo=zone)
        end_local = start_local + timedelta(days=1)
        return start_local.astimezone(UTC), end_local.astimezone(UTC)

    def delivered_today(
        self,
        profile: ClientDeliveryProfile,
        *,
        now: datetime | None = None,
    ) -> int:
        destination = ClientSheetDestinationStore(self.repository).get(
            profile.client_id, profile.destination_id, require_ready=False
        )
        start, end = self._day_window(profile, now)
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM group_deliveries "
                "WHERE client_id=? AND destination=? "
                "AND exported_at>=? AND exported_at<?",
                (
                    profile.client_id,
                    destination.logical_uri,
                    start.isoformat(),
                    end.isoformat(),
                ),
            ).fetchone()
        return int(row[0])

    def remaining_today(
        self,
        profile: ClientDeliveryProfile,
        *,
        now: datetime | None = None,
    ) -> int:
        return max(0, profile.daily_quota - self.delivered_today(profile, now=now))
