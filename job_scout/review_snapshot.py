"""Backend-authoritative review projection for one prepared client batch."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from job_scout.delivery_profiles import (
    ClientDeliveryProfileStore,
    delivery_profile_control_id,
)
from job_scout.domain.daily_batch import BatchConflict
from job_scout.domain.models import MatchDecision
from job_scout.posting_freshness import posting_freshness_disposition
from job_scout.storage.daily_batches import DailyBatchStore
from job_scout.storage.factory import create_repository


def _age_hours(posted_at: datetime | None, observed_at: datetime) -> float | None:
    if posted_at is None:
        return None
    posted = posted_at.replace(tzinfo=posted_at.tzinfo or UTC).astimezone(UTC)
    return round((observed_at - posted).total_seconds() / 3600, 1)


def review_snapshot(repository, *, profile_id: str, batch_id: str) -> dict[str, object]:
    """Project review evidence without recomputing matching in the frontend."""
    profiles = ClientDeliveryProfileStore(repository)
    batches = DailyBatchStore(repository)
    profile = profiles.get_by_control_id(profile_id)
    result = batches.get(batch_id)

    expected_profile_id = delivery_profile_control_id(
        result.request.client_id,
        result.request.destination_id or "",
    )
    if (
        result.request.client_id != profile.client_id
        or result.request.destination_id != profile.destination_id
        or expected_profile_id != profile_id.strip().casefold()
    ):
        raise BatchConflict("batch does not belong to the selected delivery profile")

    with repository.connect() as connection:
        if getattr(connection, "is_postgres", False):
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            observation = connection.execute(
                "SELECT clock_timestamp()::text AS observed_at FROM daily_batches "
                "WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
        else:
            connection.execute("BEGIN")
            observation = connection.execute(
                "SELECT strftime('%Y-%m-%dT%H:%M:%f+00:00','now') AS observed_at "
                "FROM daily_batches WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
        if observation is None:
            raise BatchConflict("batch not found")
        observed_at = datetime.fromisoformat(str(observation["observed_at"]))
        result = DailyBatchStore._load(connection, batch_id)
        if (
            result.request.client_id != profile.client_id
            or result.request.destination_id != profile.destination_id
            or delivery_profile_control_id(
                result.request.client_id,
                result.request.destination_id or "",
            )
            != profile_id.strip().casefold()
        ):
            raise BatchConflict("batch does not belong to the selected delivery profile")
        item_rows = connection.execute(
            "SELECT ordinal,representative_job_id,evidence_sha256,export_row_json "
            "FROM daily_batch_items WHERE batch_id=? ORDER BY ordinal",
            (batch_id,),
        ).fetchall()
        candidates = batches._candidates(
            connection,
            result.request.client_id,
            tuple(item["representative_job_id"] for item in item_rows),
            result.request.destination,
            result.request.match_scope_id,
        )
        candidates_by_id = {candidate.job.id: candidate for candidate in candidates}
        delivered_groups = batches._delivered_groups(
            connection,
            (candidate.group_id for candidate in candidates),
            result.request.client_id,
            result.request.destination,
        )
        journal = connection.execute(
            "SELECT export_before_sha256,export_after_sha256 FROM daily_batches "
            "WHERE batch_id=?",
            (batch_id,),
        ).fetchone()

    recovery_required = bool(
        journal
        and (
            journal["export_before_sha256"] is not None
            or journal["export_after_sha256"] is not None
        )
    )
    selectable = {MatchDecision.STRONG_MATCH, MatchDecision.POSSIBLE_MATCH}
    if result.request.include_needs_review:
        selectable.add(MatchDecision.NEEDS_REVIEW)

    items: list[dict[str, object]] = []
    all_release_ready = result.status == "prepared" and not recovery_required
    for item in item_rows:
        candidate = candidates_by_id[item["representative_job_id"]]
        export = json.loads(item["export_row_json"])
        evidence_verified = candidate.evidence_sha256 == item["evidence_sha256"]
        freshness = posting_freshness_disposition(
            posted_at=candidate.job.posted_at,
            max_age_hours=result.request.max_posting_age_hours,
            unknown_policy=result.request.unknown_posting_age_policy,
            evaluated_at=observed_at,
        )
        release_ready = (
            evidence_verified
            and freshness is None
            and not candidate.historical
            and candidate.group_id not in delivered_groups
            and candidate.match.decision in selectable
        )
        all_release_ready = all_release_ready and release_ready

        warnings: list[str] = []
        if not evidence_verified:
            warnings.append("Job or match evidence changed after this review batch was prepared.")
        if freshness == "stale_posting":
            warnings.append("This job is now older than the client's freshness limit.")
        elif freshness == "posting_age_unknown":
            warnings.append("Posting age is unknown and the client policy does not allow unknown age.")
        elif freshness == "posting_time_invalid":
            warnings.append("The posting time is invalid for delivery.")
        if candidate.historical:
            warnings.append("This job now appears in the client's prior-surfacing history.")
        if candidate.group_id in delivered_groups:
            warnings.append("An equivalent job has already been delivered to this destination.")
        if candidate.match.decision is MatchDecision.NEEDS_REVIEW:
            warnings.append("The deterministic matcher requires human review for this job.")
        elif candidate.match.decision not in selectable:
            warnings.append("This job is no longer eligible under the prepared batch policy.")

        job = candidate.job
        items.append(
            {
                "ordinal": item["ordinal"],
                "title": str(export.get("Job Title", "")).strip(),
                "company": str(export.get("Company Name", "")).strip(),
                "application_link": str(export.get("Job Link", "")).strip() or None,
                "source": str(export.get("Job Platform", "")).strip() or job.source,
                "posted_at": job.posted_at.isoformat() if job.posted_at else None,
                "age_hours": _age_hours(job.posted_at, observed_at),
                "location": job.location_text
                or ", ".join(part for part in (job.city, job.region, job.country) if part)
                or None,
                "remote_status": job.remote_status.value,
                "decision": candidate.match.decision.value,
                "matched_reasons": (
                    list(candidate.match.matched_reasons) if evidence_verified else []
                ),
                "review_reasons": (
                    list(candidate.match.rejection_reasons) if evidence_verified else []
                ),
                "evidence_verified": evidence_verified,
                "release_ready": release_ready,
                "warnings": warnings,
            }
        )

    return {
        "schema_version": "operator-review-v1",
        "observed_at": observed_at.isoformat(),
        "profile_id": profile_id.strip().casefold(),
        "batch_id": result.batch_id,
        "generation_id": result.generation_id,
        "batch_status": result.status,
        "selected_count": result.selected_count,
        "requested_quota": result.request.requested_quota,
        "freshness_limit_hours": result.request.max_posting_age_hours,
        "safe_to_release": all_release_ready,
        "recovery_required": recovery_required,
        "error": result.error,
        "items": items,
    }


def main() -> None:
    profile_id = os.getenv("JOBSIFT_PROFILE_CONTROL_ID", "").strip()
    batch_id = os.getenv("JOBSIFT_REVIEW_BATCH_ID", "").strip()
    if not profile_id or not batch_id:
        raise SystemExit("profile and batch identifiers are required")
    repository = create_repository(Path(os.getenv("JOBSIFT_DATABASE", "/tmp/jobsift.sqlite3")))
    payload = review_snapshot(repository, profile_id=profile_id, batch_id=batch_id)
    print(
        "JOBSIFT_REVIEW_STATE="
        + json.dumps(payload, sort_keys=True, separators=(",", ":"))
    )


if __name__ == "__main__":
    main()
