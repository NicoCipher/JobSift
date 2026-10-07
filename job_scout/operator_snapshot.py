"""Authoritative, non-identifying operator state projection.

This module reads durable JobSift state and prints a bounded snapshot that the
operator frontend can safely consume through private GitHub Actions logs.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from job_scout.delivery_destinations import (
    ClientSheetDestinationStore,
    logical_destination,
)
from job_scout.delivery_profiles import (
    ClientDeliveryProfile,
    ClientDeliveryProfileStore,
    delivery_profile_control_id,
)
from job_scout.normalization.core import canonicalize_url
from job_scout.storage.daily_batches import DailyBatchStore
from job_scout.storage.factory import create_repository

MAX_PROFILE_SNAPSHOTS = 100
MAX_BATCH_PREVIEW_ITEMS = 50


def _profile_from_row(row) -> ClientDeliveryProfile:
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


def _day_window(
    profile: ClientDeliveryProfile,
    observed_at: datetime,
) -> tuple[datetime, datetime]:
    zone = ZoneInfo(profile.timezone)
    local = observed_at.astimezone(zone)
    start_local = datetime(local.year, local.month, local.day, tzinfo=zone)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def _delivered_today(
    connection,
    *,
    profile: ClientDeliveryProfile,
    destination: str,
    observed_at: datetime,
) -> int:
    start, end = _day_window(profile, observed_at)
    links = {
        row["normalized_url"]
        for row in connection.execute(
            "SELECT normalized_url FROM destination_observed_links "
            "WHERE client_id=? AND destination=? "
            "AND first_observed_at>=? AND first_observed_at<?",
            (
                profile.client_id,
                destination,
                start.isoformat(),
                end.isoformat(),
            ),
        ).fetchall()
    }
    for row in connection.execute(
        "SELECT i.export_row_json FROM daily_batch_items i "
        "JOIN daily_batches b ON b.batch_id=i.batch_id "
        "WHERE b.client_id=? AND b.destination=? "
        "AND b.status='delivered' "
        "AND b.delivered_at>=? AND b.delivered_at<?",
        (
            profile.client_id,
            destination,
            start.isoformat(),
            end.isoformat(),
        ),
    ).fetchall():
        export = json.loads(row["export_row_json"])
        link = str(export.get("Job Link", "")).strip()
        if link:
            links.add(canonicalize_url(link))
    return len(links)


def _pending_batch(connection, *, client_id: str, destination: str):
    row = connection.execute(
        "SELECT batch_id,status,assembled_at,requested_quota,selected_count,"
        "shortfall,counts_json,error,export_before_sha256,export_after_sha256 "
        "FROM daily_batches WHERE client_id=? AND destination=? "
        "AND status!='delivered' AND (status='prepared' "
        "OR export_before_sha256 IS NOT NULL OR export_after_sha256 IS NOT NULL) "
        "ORDER BY assembled_at,batch_id LIMIT 1",
        (client_id, destination),
    ).fetchone()
    if row is None:
        return None

    counts = json.loads(row["counts_json"])
    item_rows = connection.execute(
        "SELECT ordinal,export_row_json FROM daily_batch_items "
        "WHERE batch_id=? ORDER BY ordinal LIMIT ?",
        (row["batch_id"], MAX_BATCH_PREVIEW_ITEMS),
    ).fetchall()
    preview = []
    for item in item_rows:
        export = json.loads(item["export_row_json"])
        preview.append(
            {
                "ordinal": item["ordinal"],
                "title": str(export.get("Job Title", "")).strip(),
                "company": str(export.get("Company Name", "")).strip(),
                "link": str(export.get("Job Link", "")).strip(),
                "platform": str(export.get("Job Platform", "")).strip(),
            }
        )

    allowed_counts = {
        key: counts.get(key)
        for key in (
            "match_eligible_postings",
            "needs_review_postings",
            "selection_eligible_postings",
            "fresh_eligible_employers",
            "historically_suppressed_groups",
            "previously_delivered_groups",
            "duplicate_postings_collapsed",
            "fresh_eligible_groups",
            "employer_cooldown_suppressed_groups",
            "stale_posting_suppressed_groups",
            "unknown_age_suppressed_groups",
            "invalid_time_suppressed_groups",
            "company_cap_suppressed_groups",
            "selected_groups",
        )
        if isinstance(counts.get(key), int)
    }
    return {
        "batch_id": row["batch_id"],
        "status": row["status"],
        "assembled_at": row["assembled_at"],
        "requested_quota": row["requested_quota"],
        "selected_count": row["selected_count"],
        "shortfall": row["shortfall"],
        "counts": allowed_counts,
        "preview": preview,
        "preview_truncated": row["selected_count"] > len(preview),
        "recovery_required": (
            row["export_before_sha256"] is not None
            or row["export_after_sha256"] is not None
        ),
        "error": row["error"],
    }


def snapshot(repository) -> dict[str, object]:
    # Initializers are intentionally outside the read transaction. SQLite tests
    # may need local schema setup; production Postgres treats these scripts as
    # no-ops and validates its versioned schema in the repository constructor.
    ClientDeliveryProfileStore(repository)
    ClientSheetDestinationStore(repository)
    DailyBatchStore(repository)

    with repository.connect() as connection:
        if getattr(connection, "is_postgres", False):
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
        else:
            connection.execute("BEGIN")

        # The observation clock and the first table read happen in the same
        # statement, which pins the transaction snapshot at the timestamp we
        # publish. This avoids ordering a stale read ahead of a newer mutation.
        if getattr(connection, "is_postgres", False):
            observation = connection.execute(
                "SELECT clock_timestamp()::text AS observed_at,COUNT(*) AS profile_count "
                "FROM client_delivery_profiles"
            ).fetchone()
        else:
            observation = connection.execute(
                "SELECT strftime('%Y-%m-%dT%H:%M:%f+00:00','now') AS observed_at,"
                "COUNT(*) AS profile_count FROM client_delivery_profiles"
            ).fetchone()
        if observation is None:
            raise RuntimeError("operator state observation boundary is unavailable")
        observed_at = datetime.fromisoformat(str(observation["observed_at"]))
        profile_rows = connection.execute(
            "SELECT * FROM client_delivery_profiles "
            "ORDER BY client_id,destination_id LIMIT ?",
            (MAX_PROFILE_SNAPSHOTS + 1,),
        ).fetchall()
        truncated = len(profile_rows) > MAX_PROFILE_SNAPSHOTS

        if getattr(connection, "is_postgres", False):
            operator_table = connection.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name='operator_clients') AS present"
            ).fetchone()
            has_operator_clients = bool(operator_table and operator_table["present"])
        else:
            operator_table = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='operator_clients'"
            ).fetchone()
            has_operator_clients = operator_table is not None

        rows: list[dict[str, object]] = []
        for profile_row in profile_rows[:MAX_PROFILE_SNAPSHOTS]:
            profile = _profile_from_row(profile_row)
            destination_row = connection.execute(
                "SELECT status,spreadsheet_id,sheet_id FROM client_sheet_destinations "
                "WHERE client_id=? AND destination_id=?",
                (profile.client_id, profile.destination_id),
            ).fetchone()
            if destination_row is None:
                raise RuntimeError("delivery profile destination is missing")

            destination = logical_destination(profile.destination_id)
            operator_row = (
                connection.execute(
                    "SELECT display_name,destination_name FROM operator_clients "
                    "WHERE client_id=? AND destination_id=?",
                    (profile.client_id, profile.destination_id),
                ).fetchone()
                if has_operator_clients
                else None
            )
            rows.append(
                {
                    "profile_id": delivery_profile_control_id(
                        profile.client_id, profile.destination_id
                    ),
                    "profile_status": profile.status,
                    "delivery_mode": profile.delivery_mode,
                    "daily_quota": profile.daily_quota,
                    "timezone": profile.timezone,
                    "sheet_status": destination_row["status"],
                    "operator_managed": operator_row is not None,
                    "client_name": (
                        operator_row["display_name"] if operator_row is not None else None
                    ),
                    "destination_name": (
                        operator_row["destination_name"] if operator_row is not None else None
                    ),
                    "sheet_url": (
                        "https://docs.google.com/spreadsheets/d/"
                        f"{destination_row['spreadsheet_id']}/edit#gid="
                        f"{destination_row['sheet_id']}"
                        if operator_row is not None
                        else None
                    ),
                    "delivered_today": _delivered_today(
                        connection,
                        profile=profile,
                        destination=destination,
                        observed_at=observed_at,
                    ),
                    "pending_batch": _pending_batch(
                        connection,
                        client_id=profile.client_id,
                        destination=destination,
                    ),
                }
            )

    return {
        "schema_version": "operator-state-v1",
        "observed_at": observed_at.isoformat(),
        "control_request_id": (
            os.getenv("JOBSIFT_CONTROL_REQUEST_ID", "").strip() or None
        ),
        "profiles": rows,
        "truncated": truncated,
    }


def main() -> None:
    database_path = Path(os.getenv("JOBSIFT_DATABASE", "/tmp/jobsift.sqlite3"))
    repository = create_repository(database_path)
    print(
        "JOBSIFT_OPERATOR_STATE="
        + json.dumps(snapshot(repository), sort_keys=True, separators=(",", ":"))
    )


if __name__ == "__main__":
    main()
