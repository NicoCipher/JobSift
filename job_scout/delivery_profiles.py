"""Persistent per-client delivery controls.

A delivery profile binds one registered client-owned destination to the
operator-controlled quota and delivery behaviour. Sourcing remains independent;
profiles decide how much of the fresh shared inventory may be delivered.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from job_scout.delivery_destinations import ClientSheetDestinationStore
from job_scout.domain.daily_batch import BatchConflict
from job_scout.normalization.core import canonicalize_url

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
            observed_rows = connection.execute(
                "SELECT normalized_url,source,source_board_id,source_job_id "
                "FROM destination_observed_links "
                "WHERE client_id=? AND destination=? "
                "AND first_observed_at>=? AND first_observed_at<?",
                (
                    profile.client_id,
                    destination.logical_uri,
                    start.isoformat(),
                    end.isoformat(),
                ),
            ).fetchall()

            # A delivered batch freezes the exact Job Link and practical delivery
            # group. Use that immutable evidence to associate a reconciled Sheet
            # URL with its delivery identity even if the current posting changes.
            frozen_groups: dict[str, str] = {}
            has_batches = (
                connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='daily_batches'"
                ).fetchone()
                is not None
            )
            if has_batches:
                for row in connection.execute(
                    "SELECT i.delivery_group_id,i.export_row_json "
                    "FROM daily_batch_items i "
                    "JOIN daily_batches b ON b.batch_id=i.batch_id "
                    "WHERE b.client_id=? AND b.destination=? "
                    "AND b.status='delivered'",
                    (profile.client_id, destination.logical_uri),
                ).fetchall():
                    export = json.loads(row["export_row_json"])
                    link = str(export.get("Job Link", "")).strip()
                    if link:
                        frozen_groups[canonicalize_url(link)] = row["delivery_group_id"]

            identities: set[tuple[str, str]] = set()
            for row in observed_rows:
                normalized_url = row["normalized_url"]
                group_id = frozen_groups.get(normalized_url)

                if (
                    group_id is None
                    and row["source"]
                    and row["source_board_id"]
                    and row["source_job_id"]
                ):
                    match = connection.execute(
                        "SELECT g.group_id FROM jobs j "
                        "JOIN posting_delivery_groups g ON g.job_id=j.id "
                        "WHERE j.source=? AND j.source_board_id=? "
                        "AND j.source_job_id=? LIMIT 1",
                        (
                            row["source"],
                            row["source_board_id"],
                            row["source_job_id"],
                        ),
                    ).fetchone()
                    if match is not None:
                        group_id = match["group_id"]

                if group_id is None:
                    match = connection.execute(
                        "SELECT g.group_id FROM delivery_keys k "
                        "JOIN posting_delivery_groups g ON g.job_id=k.job_id "
                        "WHERE k.kind='url' AND k.value=? "
                        "ORDER BY g.group_id LIMIT 1",
                        (normalized_url,),
                    ).fetchone()
                    if match is not None:
                        group_id = match["group_id"]

                identities.add(
                    ("group", group_id)
                    if group_id is not None
                    else ("url", normalized_url)
                )

            # Journaled deliveries count immediately, before the next Sheet
            # reconciliation. Once their row is observed, both sides collapse to
            # the same practical delivery-group identity instead of two URLs.
            for row in connection.execute(
                "SELECT group_id FROM group_deliveries "
                "WHERE client_id=? AND destination=? "
                "AND exported_at>=? AND exported_at<?",
                (
                    profile.client_id,
                    destination.logical_uri,
                    start.isoformat(),
                    end.isoformat(),
                ),
            ).fetchall():
                identities.add(("group", row["group_id"]))

        return len(identities)

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
        destination = ClientSheetDestinationStore(self.repository).get(
            profile.client_id, profile.destination_id
        )
        from job_scout.export.batch_sheets import ClientSheetPublisher
        from job_scout.storage.daily_batches import DailyBatchStore

        batch_store = DailyBatchStore(self.repository)
        batch = batch_store.get(batch_id)
        if (
            batch.request.client_id != profile.client_id
            or batch.request.destination_id != profile.destination_id
            or batch.request.destination != destination.logical_uri
        ):
            raise BatchConflict("batch does not belong to the selected delivery profile")

        reconciliation = self.reconcile_destination_sheet(profile, gateway=gateway)
        remaining = self.remaining_today(profile)
        if batch.status == "delivered":
            return batch, reconciliation, remaining

        # If the Sheet append already happened but the response was lost, recovery
        # must commit the existing delivery journal even when reconciliation has
        # reduced the remaining quota to zero. This path performs no new append:
        # finalize will observe the recorded after-digest and only complete the
        # delivery ledger.
        _before, after = batch_store.export_journal(batch_id)
        if after is not None:
            publisher = ClientSheetPublisher(batch, gateway, destination)
            if publisher.inspect() == after:
                return batch, reconciliation, remaining

        if profile.status != "active":
            raise BatchConflict("delivery profile is paused")
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
