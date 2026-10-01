"""Persistent per-client delivery controls.

A delivery profile binds one registered client-owned destination to the
operator-controlled quota and delivery behaviour. Sourcing remains independent;
profiles decide how much of the fresh shared inventory may be delivered.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
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


def delivery_profile_control_id(client_id: str, destination_id: str) -> str:
    """Return a stable non-identifying operator handle for public controls."""
    payload = f"jobsift-delivery-profile-v1\0{client_id}\0{destination_id}".encode()
    return sha256(payload).hexdigest()[:16]


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

    def get_by_control_id(self, control_id: str) -> ClientDeliveryProfile:
        value = control_id.strip().casefold()
        if len(value) != 16 or any(ch not in "0123456789abcdef" for ch in value):
            raise BatchConflict("invalid delivery profile control ID")
        matches = [
            profile
            for profile in self.list()
            if delivery_profile_control_id(
                profile.client_id, profile.destination_id
            ) == value
        ]
        if len(matches) != 1:
            raise BatchConflict("delivery profile control ID was not found uniquely")
        return matches[0]

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

    def update_controls(
        self,
        profile: ClientDeliveryProfile,
        *,
        daily_quota: int | None = None,
        status: Literal["active", "paused"] | None = None,
        delivery_mode: Literal["review", "auto"] | None = None,
        timezone: str | None = None,
    ) -> ClientDeliveryProfile:
        return self.upsert(
            client_id=profile.client_id,
            destination_id=profile.destination_id,
            sourcing_plan_id=profile.sourcing_plan_id,
            daily_quota=daily_quota if daily_quota is not None else profile.daily_quota,
            status=status or profile.status,
            delivery_mode=delivery_mode or profile.delivery_mode,
            timezone=timezone or profile.timezone,
        )

    def public_status(
        self,
        profile: ClientDeliveryProfile,
        *,
        now: datetime | None = None,
    ) -> dict[str, object]:
        delivered = self.delivered_today(profile, now=now)
        return {
            "profile_id": delivery_profile_control_id(
                profile.client_id, profile.destination_id
            ),
            "daily_quota": profile.daily_quota,
            "status": profile.status,
            "delivery_mode": profile.delivery_mode,
            "timezone": profile.timezone,
            "delivered_today": delivered,
            "remaining_today": max(0, profile.daily_quota - delivered),
        }

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
                "SELECT COUNT(*) FROM ("
                "SELECT normalized_url AS url FROM destination_observed_links "
                "WHERE client_id=? AND destination=? "
                "AND first_observed_at>=? AND first_observed_at<? "
                "UNION "
                "SELECT j.canonical_url AS url FROM group_deliveries d "
                "JOIN jobs j ON j.id=d.job_id "
                "WHERE d.client_id=? AND d.destination=? "
                "AND d.exported_at>=? AND d.exported_at<?"
                ")",
                (
                    profile.client_id,
                    destination.logical_uri,
                    start.isoformat(),
                    end.isoformat(),
                    profile.client_id,
                    destination.logical_uri,
                    start.isoformat(),
                    end.isoformat(),
                ),
            ).fetchone()
        return int(row[0])

    def reconcile_destination_sheet(
        self,
        profile: ClientDeliveryProfile,
        *,
        gateway,
    ) -> dict[str, int]:
        """Observe every existing Job Link before preparing another delivery."""
        destination = ClientSheetDestinationStore(self.repository).get(
            profile.client_id, profile.destination_id
        )
        metadata = gateway.sheet_metadata(destination.spreadsheet_id)
        matches = [
            item
            for item in metadata
            if item.get("sheet_id") == destination.sheet_id
        ]
        if len(matches) != 1:
            raise BatchConflict("registered Google Sheet tab no longer exists")
        if matches[0].get("title") != destination.tab_name:
            raise BatchConflict(
                "Google Sheet tab was renamed; refresh the destination registration"
            )

        values = gateway.read_table_rows(
            destination.spreadsheet_id, destination.tab_name
        )
        if not values or tuple(values[0]) != destination.header:
            raise BatchConflict(
                "Google Sheets header differs from the registered client schema"
            )
        width = len(destination.header)
        if any(len(row) > width for row in values[1:]):
            raise BatchConflict(
                "Google Sheets rows exceed the registered header width"
            )
        link_header = destination.column_mapping["Job Link"]
        link_index = destination.header.index(link_header)
        links = []
        for row in values[1:]:
            padded = row + [""] * (width - len(row))
            value = str(padded[link_index]).strip()
            if value:
                links.append(value)

        inserted, observed = self.repository.observe_destination_links(
            client_id=profile.client_id,
            destination=destination.logical_uri,
            links=links,
        )
        return {
            "observed_links": observed,
            "newly_recorded_links": inserted,
        }

    def guard_batch_release(
        self,
        profile: ClientDeliveryProfile,
        batch_id: str,
        *,
        gateway,
    ):
        """Reconcile the destination and enforce current profile controls before release."""
        if profile.status != "active":
            raise BatchConflict("delivery profile is paused")
        destination = ClientSheetDestinationStore(self.repository).get(
            profile.client_id, profile.destination_id
        )
        from job_scout.storage.daily_batches import DailyBatchStore

        batch = DailyBatchStore(self.repository).get(batch_id)
        if (
            batch.request.client_id != profile.client_id
            or batch.request.destination_id != profile.destination_id
            or batch.request.destination != destination.logical_uri
        ):
            raise BatchConflict("batch does not belong to the selected delivery profile")
        reconciliation = self.reconcile_destination_sheet(profile, gateway=gateway)
        if batch.status == "delivered":
            return batch, reconciliation, self.remaining_today(profile)
        remaining = self.remaining_today(profile)
        if batch.selected_count > remaining:
            raise BatchConflict(
                "prepared batch exceeds the profile's current remaining daily quota"
            )
        return batch, reconciliation, remaining

    def remaining_today(
        self,
        profile: ClientDeliveryProfile,
        *,
        now: datetime | None = None,
    ) -> int:
        return max(0, profile.daily_quota - self.delivered_today(profile, now=now))
