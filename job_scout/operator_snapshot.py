"""Authoritative, non-identifying operator state projection.

This module reads durable JobSift state and prints a bounded snapshot that the
operator frontend can safely consume through private GitHub Actions logs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from job_scout.delivery_destinations import ClientSheetDestinationStore
from job_scout.delivery_profiles import (
    ClientDeliveryProfileStore,
    delivery_profile_control_id,
)
from job_scout.storage.daily_batches import DailyBatchStore
from job_scout.storage.factory import create_repository

MAX_PROFILE_SNAPSHOTS = 100
MAX_BATCH_PREVIEW_ITEMS = 50


def _pending_batch(repository, *, client_id: str, destination: str):
    with repository.connect() as connection:
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
    with repository.connect() as connection:
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
    profile_store = ClientDeliveryProfileStore(repository)
    DailyBatchStore(repository)
    profiles = profile_store.list()
    destinations = ClientSheetDestinationStore(repository)
    rows: list[dict[str, object]] = []
    for profile in profiles[:MAX_PROFILE_SNAPSHOTS]:
        destination = destinations.get(
            profile.client_id, profile.destination_id, require_ready=False
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
                "sheet_status": destination.status,
                "delivered_today": profile_store.delivered_today(profile),
                "pending_batch": _pending_batch(
                    repository,
                    client_id=profile.client_id,
                    destination=destination.logical_uri,
                ),
            }
        )
    return {
        "schema_version": "operator-state-v1",
        "control_request_id": (
            os.getenv("JOBSIFT_CONTROL_REQUEST_ID", "").strip() or None
        ),
        "profiles": rows,
        "truncated": len(profiles) > MAX_PROFILE_SNAPSHOTS,
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
