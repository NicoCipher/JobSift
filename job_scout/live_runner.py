"""Stateful live JobSift runner for one verified sourcing plan and Sheets destination."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from job_scout.delivery_destinations import ClientSheetDestinationStore
from job_scout.delivery_profiles import ClientDeliveryProfileStore
from job_scout.domain.daily_batch import BatchConflict, DailyBatchRequest, DailyBatchResult
from job_scout.export.batch_sheets import GoogleSheetsGateway, sheet_destination
from job_scout.orchestration.daily_batch import finalize_daily_batch, prepare_daily_batch
from job_scout.search_brief import load_search_brief
from job_scout.sourcing_plan import (
    SourcingPlan,
    collect_inventory_plan,
    evaluate_inventory_run,
    evaluate_recent_inventory,
    load_sourcing_plan,
)
from job_scout.storage.daily_batches import DailyBatchStore
from job_scout.storage.inventory_runs import InventoryRunStore
from job_scout.storage.sqlite import SQLiteRepository


def _boolean(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().casefold()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _positive_integer(name: str, default: int) -> int:
    raw = os.getenv(name)
    value = default if raw is None else int(raw)
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


@dataclass(frozen=True)
class LiveRunnerConfig:
    plan_path: Path
    database_path: Path
    csv_path: Path
    reports_dir: Path
    spreadsheet_id: str | None
    sheet_tab: str | None
    quota: int
    interval_seconds: int
    timezone: str
    auto_release: bool
    allow_partial: bool
    run_once: bool
    destination_id: str | None = None
    discard_prepared: bool = False
    validation_only: bool = False
    inventory_retention_hours: int = 72
    profile_managed: bool = False
    source_before_delivery: bool = True

    @classmethod
    def from_env(cls) -> LiveRunnerConfig:
        timezone = os.getenv("JOBSIFT_TIMEZONE", "Africa/Lagos").strip() or "Africa/Lagos"
        ZoneInfo(timezone)
        auto_release = _boolean("JOBSIFT_AUTO_RELEASE")
        discard_prepared = _boolean("JOBSIFT_DISCARD_PREPARED")
        validation_only = _boolean("JOBSIFT_VALIDATION_ONLY")
        if auto_release and discard_prepared:
            raise ValueError("release and discard modes cannot both be enabled")
        if validation_only and (auto_release or discard_prepared):
            raise ValueError("validation mode cannot release or discard production batches")
        destination_id = os.getenv("JOBSIFT_DESTINATION_ID", "").strip() or None
        spreadsheet_id = os.getenv("JOBSIFT_SHEET_ID", "").strip() or None
        sheet_tab = os.getenv("JOBSIFT_SHEET_TAB", "").strip() or None
        if (spreadsheet_id is None) != (sheet_tab is None):
            raise ValueError(
                "JOBSIFT_SHEET_ID and JOBSIFT_SHEET_TAB must be configured together"
            )
        return cls(
            plan_path=Path(
                os.getenv(
                    "JOBSIFT_PLAN",
                    "config/sourcing_plans/taiwo_software_remote_us_pilot_v1.json",
                )
            ),
            database_path=Path(_required("JOBSIFT_DATABASE")),
            csv_path=Path(_required("JOBSIFT_CSV")),
            reports_dir=Path(os.getenv("JOBSIFT_REPORTS_DIR", "/var/data/runs")),
            spreadsheet_id=spreadsheet_id,
            sheet_tab=sheet_tab,
            destination_id=destination_id,
            quota=_positive_integer("JOBSIFT_BATCH_QUOTA", 5),
            interval_seconds=_positive_integer("JOBSIFT_INTERVAL_SECONDS", 86400),
            timezone=timezone,
            auto_release=auto_release,
            allow_partial=_boolean("JOBSIFT_ALLOW_PARTIAL"),
            run_once=_boolean("JOBSIFT_RUN_ONCE"),
            discard_prepared=discard_prepared,
            validation_only=validation_only,
            inventory_retention_hours=_positive_integer(
                "JOBSIFT_INVENTORY_RETENTION_HOURS", 72
            ),
            profile_managed=_boolean("JOBSIFT_PROFILE_MANAGED"),
            source_before_delivery=_boolean("JOBSIFT_SOURCE_BEFORE_DELIVERY", True),
        )


def _resolved(path_value: str, base_dir: Path) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else (base_dir / path).resolve()


def _runtime_plan(config: LiveRunnerConfig) -> tuple[SourcingPlan, Path]:
    plan_path = config.plan_path.resolve()
    plan = load_sourcing_plan(plan_path)
    plan = plan.model_copy(
        update={
            "database": str(config.database_path.resolve()),
            "csv": str(config.csv_path.resolve()),
        }
    )
    brief_path = _resolved(plan.search_brief, plan_path.parent)
    return plan, brief_path


def _batch_by_idempotency(
    repository: SQLiteRepository,
    *,
    client_id: str,
    destination: str,
    idempotency_key: str,
) -> DailyBatchResult | None:
    with repository.connect() as connection:
        row = connection.execute(
            "SELECT batch_id FROM daily_batches "
            "WHERE client_id=? AND destination=? AND idempotency_key=?",
            (client_id, destination, idempotency_key),
        ).fetchone()
    return DailyBatchStore(repository).get(row[0]) if row else None


def _unresolved_batch(
    repository: SQLiteRepository, *, client_id: str, destination: str
) -> DailyBatchResult | None:
    with repository.connect() as connection:
        row = connection.execute(
            "SELECT batch_id FROM daily_batches "
            "WHERE client_id=? AND destination=? AND status!='delivered' "
            "AND (status='prepared' OR export_after_sha256 IS NOT NULL) "
            "ORDER BY assembled_at, batch_id LIMIT 1",
            (client_id, destination),
        ).fetchone()
    return DailyBatchStore(repository).get(row[0]) if row else None


def _discard_stale_unpublished_release(
    store: DailyBatchStore, batch: DailyBatchResult
) -> bool:
    """Discard only a freshness-failed snapshot that never reached an export journal."""
    if batch.status != "failed" or not (batch.error or "").startswith(
        "prepared posting is no longer fresh at delivery:"
    ):
        return False
    before_sha, after_sha = store.export_journal(batch.batch_id)
    if before_sha is not None or after_sha is not None or batch.delivered_at is not None:
        return False
    store.discard_prepared(batch.batch_id)
    return True


def _candidate_job_ids(
    repository: SQLiteRepository,
    *,
    client_id: str,
    plan: SourcingPlan,
    started_at: datetime,
    completed_at: datetime,
) -> tuple[str, ...]:
    target_pairs = sorted(
        {(target.source, target.source_target().board_id) for target in plan.targets}
    )
    clauses = " OR ".join("(j.source=? AND j.source_board_id=?)" for _ in target_pairs)
    params: list[str] = [
        client_id,
        started_at.astimezone(UTC).isoformat(),
        completed_at.astimezone(UTC).isoformat(),
    ]
    for source, board_id in target_pairs:
        params.extend((source, board_id))
    with repository.connect() as connection:
        rows = connection.execute(
            "SELECT j.id FROM jobs j JOIN job_matches m ON m.job_id=j.id "
            "WHERE m.client_id=? AND m.evaluated_at>=? AND m.evaluated_at<=? "
            f"AND ({clauses}) ORDER BY j.id",
            params,
        ).fetchall()
    return tuple(row[0] for row in rows)


def _source_failures(report) -> tuple[str, ...]:
    failures = []
    for target in report.targets:
        if target.status == "success":
            continue
        detail = "; ".join(target.errors) if target.errors else target.status
        failures.append(f"{target.target_identity}: {detail}")
    return tuple(failures)


def _batch_payload(
    store: DailyBatchStore,
    batch: DailyBatchResult,
    *,
    action: str,
    sourcing=None,
    evaluation=None,
    retention=None,
) -> dict[str, object]:
    rows = store.export_rows(batch.batch_id)
    payload: dict[str, object] = {
        "action": action,
        "batch_id": batch.batch_id,
        "batch_status": batch.status,
        "destination_id": getattr(batch.request, "destination_id", None),
        "requested_quota": batch.request.requested_quota,
        "selected_count": batch.selected_count,
        "shortfall": batch.shortfall,
        "fresh_eligible_employers": batch.counts.fresh_eligible_employers,
        "company_cap_suppressed_groups": batch.counts.company_cap_suppressed_groups,
        "employer_cooldown_suppressed_groups": batch.counts.employer_cooldown_suppressed_groups,
        "stale_posting_suppressed_groups": batch.counts.stale_posting_suppressed_groups,
        "unknown_age_suppressed_groups": batch.counts.unknown_age_suppressed_groups,
        "invalid_time_suppressed_groups": batch.counts.invalid_time_suppressed_groups,
        "completeness": batch.request.completeness,
        "error": batch.error,
        "selected_jobs": [
            {
                "title": row["Job Title"],
                "company": row["Company Name"],
                "link": row["Job Link"],
                "platform": row["Job Platform"],
            }
            for row in rows
        ],
    }
    if retention is not None:
        payload["retention"] = retention
    if sourcing is not None:
        payload["sourcing"] = {
            "status": sourcing.status,
            "inventory_run_id": getattr(sourcing, "run_id", None),
            "received": sourcing.total_received,
            "matched": (
                evaluation.total_matched
                if evaluation is not None
                else getattr(sourcing, "total_matched", 0)
            ),
            "rejected": (
                evaluation.total_rejected
                if evaluation is not None
                else getattr(sourcing, "total_rejected", 0)
            ),
            "successful_targets": sourcing.successful_targets,
            "partial_targets": sourcing.partial_targets,
            "failed_targets": sourcing.failed_targets,
        }
    return payload


def run_once(config: LiveRunnerConfig) -> dict[str, object]:
    config.database_path.parent.mkdir(parents=True, exist_ok=True)
    config.csv_path.parent.mkdir(parents=True, exist_ok=True)
    config.reports_dir.mkdir(parents=True, exist_ok=True)

    plan, brief_path = _runtime_plan(config)
    brief = load_search_brief(brief_path)
    repository = SQLiteRepository(config.database_path)
    if config.validation_only and repository.remote_url:
        raise ValueError("validation mode must use a local-only repository")
    store = DailyBatchStore(repository)
    destination_record = None
    if config.validation_only:
        destination = (
            sheet_destination(config.spreadsheet_id, config.sheet_tab)
            if config.spreadsheet_id is not None and config.sheet_tab is not None
            else sheet_destination("validation", "Validation")
        )
    else:
        destination_store = ClientSheetDestinationStore(repository)
        if config.destination_id is not None:
            destination_record = destination_store.get(
                brief.client_id, config.destination_id
            )
        else:
            ready_destinations = tuple(
                value
                for value in destination_store.list(brief.client_id)
                if value.status == "ready"
            )
            if len(ready_destinations) == 1:
                destination_record = ready_destinations[0]
            elif len(ready_destinations) > 1:
                raise ValueError(
                    "multiple ready client destinations exist; set JOBSIFT_DESTINATION_ID"
                )

        if destination_record is not None:
            destination = destination_record.logical_uri
        elif config.spreadsheet_id is not None and config.sheet_tab is not None:
            destination = sheet_destination(config.spreadsheet_id, config.sheet_tab)
        else:
            raise ValueError(
                "no ready client destination and no legacy Google Sheet fallback configured"
            )

    profile = None
    profile_store = None
    delivered_today = 0
    effective_quota = config.quota
    profile_auto_release = False
    profile_timezone = config.timezone
    if config.profile_managed:
        if config.validation_only:
            raise ValueError("profile-managed delivery cannot run in validation mode")
        if destination_record is None:
            raise ValueError(
                "profile-managed delivery requires a registered client destination"
            )
        profile_store = ClientDeliveryProfileStore(repository)
        profile = profile_store.get(brief.client_id, destination_record.destination_id)
        if profile.sourcing_plan_id != plan.plan_id:
            raise ValueError(
                "delivery profile sourcing plan does not match the active runner plan"
            )
        profile_timezone = profile.timezone
        delivered_today = profile_store.delivered_today(profile)
        effective_quota = max(0, profile.daily_quota - delivered_today)
        profile_auto_release = profile.delivery_mode == "auto"
    unresolved = _unresolved_batch(
        repository, client_id=brief.client_id, destination=destination
    )
    if unresolved is not None:
        # The legacy Live JobSift release workflow predates delivery profiles.
        # If it is releasing a batch for a destination that now has a profile,
        # adopt that profile for this release so pause/quota/reconciliation
        # controls cannot be bypassed by the older entrypoint.
        if (
            profile is None
            and config.auto_release
            and getattr(unresolved.request, "destination_id", None) is not None
        ):
            legacy_profile_store = ClientDeliveryProfileStore(repository)
            try:
                legacy_profile = legacy_profile_store.get(
                    unresolved.request.client_id,
                    unresolved.request.destination_id,
                )
            except BatchConflict:
                legacy_profile = None
            if legacy_profile is not None:
                profile_store = legacy_profile_store
                profile = legacy_profile
                profile_timezone = profile.timezone
                delivered_today = profile_store.delivered_today(profile)
                effective_quota = max(0, profile.daily_quota - delivered_today)
                profile_auto_release = profile.delivery_mode == "auto"

        if config.discard_prepared:
            discarded = store.discard_prepared(unresolved.batch_id)
            return {
                "action": "discarded_prepared",
                "batch_id": discarded.batch_id,
                "selected_count": discarded.selected_count,
                "destination": destination,
            }

        # Pausing a profile or reaching today's quota must not strand an export
        # that is already visible on the Sheet after an uncertain response.
        # guard_batch_release only bypasses those controls when the current
        # destination digest proves the journaled append already happened.
        if (
            profile is not None
            and profile_store is not None
            and (profile.status == "paused" or effective_quota == 0)
        ):
            try:
                unresolved, _reconciliation, effective_quota = (
                    profile_store.guard_batch_release(
                        profile,
                        unresolved.batch_id,
                        gateway=GoogleSheetsGateway(),
                    )
                )
            except BatchConflict:
                if profile.status == "paused":
                    return {
                        "action": "profile_paused",
                        "client_id": profile.client_id,
                        "destination_id": profile.destination_id,
                        "daily_quota": profile.daily_quota,
                        "delivery_mode": profile.delivery_mode,
                    }
                return {
                    "action": "quota_reached",
                    "client_id": profile.client_id,
                    "destination_id": profile.destination_id,
                    "daily_quota": profile.daily_quota,
                    "delivered_today": delivered_today,
                    "remaining_today": 0,
                    "delivery_mode": profile.delivery_mode,
                }
            unresolved = finalize_daily_batch(
                repository=repository, batch_id=unresolved.batch_id
            )
            return _batch_payload(store, unresolved, action="recovered_release")

        if config.auto_release or profile_auto_release:
            if profile is not None and profile_store is not None:
                try:
                    unresolved, _reconciliation, effective_quota = (
                        profile_store.guard_batch_release(
                            profile,
                            unresolved.batch_id,
                            gateway=GoogleSheetsGateway(),
                        )
                    )
                except BatchConflict as error:
                    action = (
                        "release_blocked_quota"
                        if "remaining daily quota" in str(error)
                        else "release_blocked_profile"
                    )
                    return _batch_payload(store, unresolved, action=action)
            unresolved = finalize_daily_batch(
                repository=repository, batch_id=unresolved.batch_id
            )
            if _discard_stale_unpublished_release(store, unresolved):
                return {
                    "action": "stale_unpublished_discarded",
                    "batch_id": unresolved.batch_id,
                    "batch_status": unresolved.status,
                    "destination_id": getattr(
                        unresolved.request, "destination_id", None
                    ),
                    "selected_count": unresolved.selected_count,
                    "error": unresolved.error,
                }
            action = "resumed_release"
        else:
            action = "awaiting_release"
        return _batch_payload(store, unresolved, action=action)

    if profile is not None and profile.status == "paused":
        return {
            "action": "profile_paused",
            "client_id": profile.client_id,
            "destination_id": profile.destination_id,
            "daily_quota": profile.daily_quota,
            "delivery_mode": profile.delivery_mode,
        }

    if profile is not None and effective_quota == 0:
        return {
            "action": "quota_reached",
            "client_id": profile.client_id,
            "destination_id": profile.destination_id,
            "daily_quota": profile.daily_quota,
            "delivered_today": delivered_today,
            "remaining_today": 0,
            "delivery_mode": profile.delivery_mode,
        }

    if config.discard_prepared:
        return {
            "action": "nothing_to_discard",
            "plan_id": plan.plan_id,
            "client_id": brief.client_id,
            "destination": destination,
        }

    if config.auto_release:
        return {
            "action": "nothing_to_release",
            "plan_id": plan.plan_id,
            "client_id": brief.client_id,
            "destination": destination,
        }

    local_day = datetime.now(ZoneInfo(profile_timezone)).date().isoformat()
    idempotency_key = local_day
    if profile is None:
        today = _batch_by_idempotency(
            repository,
            client_id=brief.client_id,
            destination=destination,
            idempotency_key=idempotency_key,
        )
        if today is not None:
            return _batch_payload(store, today, action="already_ran_today")

    brief_sha = sha256(brief_path.read_bytes()).hexdigest()
    match_scope_id = brief_sha if profile is not None else None

    report = None
    if config.source_before_delivery:
        report = collect_inventory_plan(
            plan,
            repository=repository,
            reports_dir=config.reports_dir.resolve(),
        )
        if report.status == "failure":
            return {
                "action": "sourcing_failed",
                "plan_id": plan.plan_id,
                "status": report.status,
                "source_failures": list(_source_failures(report)),
            }
        if report.status != "success" and not config.allow_partial:
            return {
                "action": "partial_sourcing_blocked",
                "plan_id": plan.plan_id,
                "status": report.status,
                "source_failures": list(_source_failures(report)),
            }

    # Retention is maintenance, not a prerequisite for matching. Inventory reads
    # enforce the same retention window independently, so cleanup can run outside
    # the delivery critical path.
    retention = None

    if report is not None:
        evaluation = evaluate_inventory_run(
            repository=repository,
            run_id=report.run_id,
            brief=brief,
            retention_hours=config.inventory_retention_hours,
            evaluated_at=report.completed_at,
            match_scope_id=match_scope_id,
        )
        candidate_ids = InventoryRunStore(repository).active_job_ids(
            report.run_id,
            retention_hours=config.inventory_retention_hours,
            evaluated_at=report.completed_at,
        )
        scope = f"{plan.plan_id}:{report.run_id}"
        failures = _source_failures(report)
        completeness = "complete" if report.status == "success" else "partial"
    else:
        evaluation, candidate_ids = evaluate_recent_inventory(
            repository=repository,
            brief=brief,
            retention_hours=config.inventory_retention_hours,
            match_scope_id=match_scope_id,
        )
        scope = f"{plan.plan_id}:{evaluation.run_id}"
        failures = ()
        completeness = "complete"

    if profile is not None:
        scope_key = sha256(scope.encode()).hexdigest()[:20]
        idempotency_key = f"{local_day}:scope-{scope_key}"
        existing_scope = _batch_by_idempotency(
            repository,
            client_id=brief.client_id,
            destination=destination,
            idempotency_key=idempotency_key,
        )
        if existing_scope is not None:
            return _batch_payload(
                store, existing_scope, action="already_ran_inventory_scope"
            )

    evidence_sha = store.evidence_digest(
        brief.client_id,
        candidate_ids,
        match_scope_id=match_scope_id,
    )
    evaluation_id = (
        f"{scope}:{brief.client_id}:{evaluation.evaluated_at.astimezone(UTC).isoformat()}"
    )
    request = DailyBatchRequest(
        client_id=brief.client_id,
        destination=destination,
        destination_id=(destination_record.destination_id if destination_record else None),
        destination_config_sha256=(
            destination_record.config_sha256 if destination_record else None
        ),
        idempotency_key=idempotency_key,
        requested_quota=effective_quota,
        max_jobs_per_employer_per_batch=brief.delivery_policy.max_jobs_per_employer_per_batch,
        employer_cooldown_days=brief.delivery_policy.employer_cooldown_days,
        max_posting_age_hours=brief.posting_freshness.max_age_hours,
        unknown_posting_age_policy=brief.posting_freshness.unknown_policy,
        freshness_evaluated_at=(
            evaluation.evaluated_at
            if brief.posting_freshness.max_age_hours is not None
            else None
        ),
        evidence_scope_id=scope,
        evaluation_id=evaluation_id,
        candidate_job_ids=candidate_ids,
        evidence_sha256=evidence_sha,
        match_scope_id=match_scope_id,
        brief_revision_id=brief_path.stem,
        brief_sha256=brief_sha,
        completeness=completeness,
        source_failures=failures,
    )
    batch = prepare_daily_batch(repository=repository, request=request)
    action = "validated" if config.validation_only else "prepared"

    # A review-mode scope with zero selected jobs is not something an operator
    # can meaningfully approve. Do not leave it as an unresolved prepared batch,
    # otherwise it blocks later hourly inventory scopes that may contain fresh
    # matches.
    if (
        profile is not None
        and not config.validation_only
        and profile.delivery_mode == "review"
        and batch.selected_count == 0
    ):
        empty_payload = _batch_payload(
            store,
            batch,
            action="empty_review_scope",
            sourcing=report,
            evaluation=evaluation,
            retention=retention,
        )
        store.discard_prepared(batch.batch_id)
        empty_payload["batch_status"] = "discarded"
        if profile_store is not None:
            delivered_after = profile_store.delivered_today(profile)
            empty_payload["delivery_profile"] = {
                "client_id": profile.client_id,
                "destination_id": profile.destination_id,
                "daily_quota": profile.daily_quota,
                "status": profile.status,
                "delivery_mode": profile.delivery_mode,
                "timezone": profile.timezone,
                "delivered_today": delivered_after,
                "remaining_today": max(0, profile.daily_quota - delivered_after),
            }
        return empty_payload

    if profile_auto_release and profile_store is not None and profile is not None:
        current_profile = profile_store.get(profile.client_id, profile.destination_id)
        profile = current_profile
        if current_profile.sourcing_plan_id != plan.plan_id:
            action = "release_blocked_profile"
        elif current_profile.delivery_mode != "auto":
            action = "prepared"
        else:
            try:
                batch, _reconciliation, _remaining = profile_store.guard_batch_release(
                    current_profile,
                    batch.batch_id,
                    gateway=GoogleSheetsGateway(),
                )
            except BatchConflict as error:
                action = (
                    "release_blocked_quota"
                    if "remaining daily quota" in str(error)
                    else "release_blocked_profile"
                )
            else:
                batch = finalize_daily_batch(
                    repository=repository, batch_id=batch.batch_id
                )
                action = "released" if batch.status == "delivered" else "release_failed"
    payload = _batch_payload(
        store,
        batch,
        action=action,
        sourcing=report,
        evaluation=evaluation,
        retention=retention,
    )
    if profile is not None and profile_store is not None:
        delivered_after = profile_store.delivered_today(profile)
        payload["delivery_profile"] = {
            "client_id": profile.client_id,
            "destination_id": profile.destination_id,
            "daily_quota": profile.daily_quota,
            "status": profile.status,
            "delivery_mode": profile.delivery_mode,
            "timezone": profile.timezone,
            "delivered_today": delivered_after,
            "remaining_today": max(0, profile.daily_quota - delivered_after),
        }
    return payload


def main() -> None:
    config = LiveRunnerConfig.from_env()
    while True:
        try:
            result = run_once(config)
            print(json.dumps(result, sort_keys=True), flush=True)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            print(
                json.dumps(
                    {
                        "action": "runner_error",
                        "error": f"{type(error).__name__}: {error}",
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
                flush=True,
            )
            if config.run_once:
                raise
        if config.run_once:
            return
        time.sleep(config.interval_seconds)


if __name__ == "__main__":
    main()
