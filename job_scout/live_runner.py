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

from job_scout.domain.daily_batch import DailyBatchRequest, DailyBatchResult
from job_scout.export.batch_sheets import managed_sheet_destination, sheet_destination
from job_scout.orchestration.daily_batch import finalize_daily_batch, prepare_daily_batch
from job_scout.search_brief import load_search_brief
from job_scout.sourcing_plan import SourcingPlan, load_sourcing_plan, run_sourcing_plan
from job_scout.storage.daily_batches import DailyBatchStore
from job_scout.storage.destinations import DeliveryDestinationStore
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
    campaign_id: str | None = None
    discard_prepared: bool = False
    validation_only: bool = False
    inventory_retention_hours: int = 72

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
        campaign_id = os.getenv("JOBSIFT_CAMPAIGN_ID", "").strip() or None
        spreadsheet_id = os.getenv("JOBSIFT_SHEET_ID", "").strip() or None
        sheet_tab = os.getenv("JOBSIFT_SHEET_TAB", "").strip() or None
        if campaign_id is None and bool(spreadsheet_id) != bool(sheet_tab):
            raise ValueError("legacy Google Sheets delivery requires both sheet ID and tab")
        if campaign_id is None and not validation_only and not spreadsheet_id:
            raise ValueError(
                "JOBSIFT_CAMPAIGN_ID is required unless legacy sheet ID/tab are configured"
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
            campaign_id=campaign_id,
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
            "ORDER BY assembled_at, batch_id LIMIT 1",
            (client_id, destination),
        ).fetchone()
    return DailyBatchStore(repository).get(row[0]) if row else None


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
    retention=None,
) -> dict[str, object]:
    rows = store.export_rows(batch.batch_id)
    sheet_delivery = getattr(batch.request, "sheet_delivery", None)
    payload: dict[str, object] = {
        "action": action,
        "batch_id": batch.batch_id,
        "batch_status": batch.status,
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
        "campaign_id": sheet_delivery.campaign_id if sheet_delivery is not None else None,
        "destination_id": (
            sheet_delivery.destination_id if sheet_delivery is not None else None
        ),
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
            "received": sourcing.total_received,
            "matched": sourcing.total_matched,
            "rejected": sourcing.total_rejected,
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
    sheet_contract = None
    if config.validation_only:
        destination = str((config.reports_dir / "validation-output.csv").resolve())
    elif config.campaign_id is not None:
        sheet_contract = DeliveryDestinationStore(repository).resolve_campaign(
            client_id=brief.client_id,
            campaign_id=config.campaign_id,
        )
        destination = managed_sheet_destination(sheet_contract.destination_id)
    elif config.spreadsheet_id is not None and config.sheet_tab is not None:
        destination = sheet_destination(config.spreadsheet_id, config.sheet_tab)
    else:
        raise ValueError("no delivery destination is configured")

    unresolved = _unresolved_batch(
        repository, client_id=brief.client_id, destination=destination
    )
    if unresolved is not None:
        if config.discard_prepared:
            discarded = store.discard_prepared(unresolved.batch_id)
            return {
                "action": "discarded_prepared",
                "batch_id": discarded.batch_id,
                "selected_count": discarded.selected_count,
                "destination": destination,
            }
        if config.auto_release:
            unresolved = finalize_daily_batch(
                repository=repository, batch_id=unresolved.batch_id
            )
            action = "resumed_release"
        else:
            action = "awaiting_release"
        return _batch_payload(store, unresolved, action=action)

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

    local_day = datetime.now(ZoneInfo(config.timezone)).date().isoformat()
    idempotency_key = (
        f"{config.campaign_id}:{local_day}" if config.campaign_id is not None else local_day
    )
    today = _batch_by_idempotency(
        repository,
        client_id=brief.client_id,
        destination=destination,
        idempotency_key=idempotency_key,
    )
    if today is not None:
        return _batch_payload(store, today, action="already_ran_today")

    report = run_sourcing_plan(
        plan,
        base_dir=config.plan_path.resolve().parent,
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

    retention = repository.prune_stale_inventory(
        retention_hours=config.inventory_retention_hours
    )

    candidate_ids = _candidate_job_ids(
        repository,
        client_id=brief.client_id,
        plan=plan,
        started_at=report.started_at,
        completed_at=report.completed_at,
    )
    evidence_sha = store.evidence_digest(brief.client_id, candidate_ids)
    brief_sha = sha256(brief_path.read_bytes()).hexdigest()
    scope = f"{plan.plan_id}:{report.started_at.astimezone(UTC).isoformat()}"
    failures = _source_failures(report)
    completeness = "complete" if report.status == "success" else "partial"
    request = DailyBatchRequest(
        client_id=brief.client_id,
        destination=destination,
        sheet_delivery=sheet_contract,
        idempotency_key=idempotency_key,
        requested_quota=config.quota,
        max_jobs_per_employer_per_batch=brief.delivery_policy.max_jobs_per_employer_per_batch,
        employer_cooldown_days=brief.delivery_policy.employer_cooldown_days,
        max_posting_age_hours=brief.posting_freshness.max_age_hours,
        unknown_posting_age_policy=brief.posting_freshness.unknown_policy,
        freshness_evaluated_at=(
            report.completed_at if brief.posting_freshness.max_age_hours is not None else None
        ),
        evidence_scope_id=scope,
        evaluation_id=scope,
        candidate_job_ids=candidate_ids,
        evidence_sha256=evidence_sha,
        brief_revision_id=brief_path.stem,
        brief_sha256=brief_sha,
        completeness=completeness,
        source_failures=failures,
    )
    batch = prepare_daily_batch(repository=repository, request=request)
    action = "validated" if config.validation_only else "prepared"
    if config.auto_release:
        batch = finalize_daily_batch(repository=repository, batch_id=batch.batch_id)
        action = "released" if batch.status == "delivered" else "release_failed"
    return _batch_payload(
        store,
        batch,
        action=action,
        sourcing=report,
        retention=retention,
    )


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
