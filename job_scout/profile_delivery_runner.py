"""Run delivery for every active client profile from the shared inventory."""

from __future__ import annotations

import json
import os
from pathlib import Path

from job_scout.delivery_profiles import ClientDeliveryProfileStore
from job_scout.export.batch_sheets import GoogleSheetsGateway
from job_scout.live_runner import LiveRunnerConfig, run_once
from job_scout.sourcing_plan import load_sourcing_plan
from job_scout.storage.sqlite import SQLiteRepository


def resolve_plan(plan_dir: Path, plan_id: str) -> Path:
    matches: list[Path] = []
    for path in sorted(plan_dir.glob("*.json")):
        plan = load_sourcing_plan(path)
        if plan.plan_id == plan_id:
            matches.append(path.resolve())
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one sourcing plan for {plan_id!r}; found {len(matches)}"
        )
    return matches[0]


def run_active_profiles(
    *,
    database_path: Path,
    plan_dir: Path,
    reports_dir: Path,
    retention_hours: int = 72,
    control_id: str | None = None,
) -> list[dict[str, object]]:
    repository = SQLiteRepository(database_path)
    profile_store = ClientDeliveryProfileStore(repository)
    if control_id is None:
        profiles = profile_store.active()
    else:
        selected = profile_store.get_by_control_id(control_id)
        profiles = (selected,) if selected.status == "active" else ()
    results: list[dict[str, object]] = []
    gateway = GoogleSheetsGateway() if profiles else None
    for profile in profiles:
        try:
            assert gateway is not None
            reconciliation = profile_store.reconcile_destination_sheet(
                profile, gateway=gateway
            )
            plan_path = resolve_plan(plan_dir, profile.sourcing_plan_id)
            result = run_once(
                LiveRunnerConfig(
                    plan_path=plan_path,
                    database_path=database_path,
                    csv_path=Path("/tmp/jobsift-profile-delivery.csv"),
                    reports_dir=reports_dir,
                    spreadsheet_id=None,
                    sheet_tab=None,
                    quota=profile.daily_quota,
                    interval_seconds=86400,
                    timezone=profile.timezone,
                    auto_release=False,
                    allow_partial=False,
                    run_once=True,
                    destination_id=profile.destination_id,
                    inventory_retention_hours=retention_hours,
                    profile_managed=True,
                    source_before_delivery=False,
                )
            )
            result["sheet_reconciliation"] = reconciliation
            results.append(result)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            results.append(
                {
                    "action": "profile_runner_error",
                    "client_id": profile.client_id,
                    "destination_id": profile.destination_id,
                    "error": f"{type(error).__name__}: {error}",
                }
            )
    return results


def main() -> None:
    database_path = Path(os.getenv("JOBSIFT_DATABASE", "/tmp/jobsift.sqlite3"))
    plan_dir = Path(os.getenv("JOBSIFT_PLAN_DIR", "config/sourcing_plans"))
    reports_dir = Path(os.getenv("JOBSIFT_REPORTS_DIR", "/tmp/jobsift-runs"))
    retention_hours = int(os.getenv("JOBSIFT_INVENTORY_RETENTION_HOURS", "72"))
    control_id = os.getenv("JOBSIFT_PROFILE_CONTROL_ID", "").strip() or None
    if retention_hours < 1:
        raise ValueError("JOBSIFT_INVENTORY_RETENTION_HOURS must be at least 1")
    results = run_active_profiles(
        database_path=database_path,
        plan_dir=plan_dir,
        reports_dir=reports_dir,
        retention_hours=retention_hours,
        control_id=control_id,
    )
    print(json.dumps({"profiles": results}, sort_keys=True))
    if any(result.get("action") == "profile_runner_error" for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
