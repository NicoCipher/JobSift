"""Evaluate fresh shared inventory against active client delivery profiles."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime, time, timedelta
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from job_scout.delivery_destinations import ClientSheetDestination, ClientSheetDestinationStore
from job_scout.delivery_profiles import ClientDeliveryProfile, ClientDeliveryProfileStore
from job_scout.domain.daily_batch import DailyBatchRequest
from job_scout.export.batch_sheets import GoogleSheetsGateway
from job_scout.history import HistoricalRecord, operator_status, source_identity
from job_scout.normalization.core import canonicalize_url
from job_scout.orchestration.daily_batch import finalize_daily_batch, prepare_daily_batch
from job_scout.search_brief import load_search_brief
from job_scout.sourcing_plan import evaluate_inventory_run
from job_scout.storage.daily_batches import DailyBatchStore
from job_scout.storage.inventory_runs import InventoryRunStore
from job_scout.storage.sqlite import SQLiteRepository


def _destination_rows(
    destination: ClientSheetDestination,
    gateway: GoogleSheetsGateway,
) -> list[list[str]]:
    metadata = gateway.sheet_metadata(destination.spreadsheet_id)
    matches = [
        item for item in metadata if item.get("sheet_id") == destination.sheet_id
    ]
    if len(matches) != 1 or matches[0].get("title") != destination.tab_name:
        raise ValueError("registered client Sheet identity changed; refresh registration")
    values = gateway.read_table_rows(destination.spreadsheet_id, destination.tab_name)
    if not values or tuple(values[0]) != destination.header:
        raise ValueError("client Sheet header differs from registered schema")
    width = len(destination.header)
    if any(len(row) > width for row in values[1:]):
        raise ValueError("client Sheet rows exceed registered schema")
    return [row + [""] * (width - len(row)) for row in values]


def reconcile_sheet_history(
    *,
    repository: SQLiteRepository,
    profile: ClientDeliveryProfile,
    destination: ClientSheetDestination,
    gateway: GoogleSheetsGateway,
) -> dict[str, int]:
    """Treat every existing Sheet link as prior client surfacing before new delivery."""
    rows = _destination_rows(destination, gateway)
    mapping = destination.column_mapping
    link_index = destination.header.index(mapping["Job Link"])
    title_index = (
        destination.header.index(mapping["Job Title"])
        if "Job Title" in mapping
        else None
    )
    company_index = (
        destination.header.index(mapping["Company Name"])
        if "Company Name" in mapping
        else None
    )
    status_index = (
        destination.header.index(mapping["Status"])
        if "Status" in mapping
        else None
    )

    records: list[HistoricalRecord] = []
    seen: set[str] = set()
    for row_number, row in enumerate(rows[1:], 2):
        raw = row[link_index].strip()
        if not raw:
            continue
        normalized = canonicalize_url(raw)
        if normalized in seen:
            continue
        seen.add(normalized)
        source, board, job = source_identity(raw)
        records.append(
            HistoricalRecord(
                original_url=raw,
                normalized_url=normalized,
                source=source,
                source_board_id=board,
                source_job_id=job,
                title=row[title_index].strip() if title_index is not None else "",
                company=row[company_index].strip() if company_index is not None else "",
                operator_status=(
                    operator_status(row[status_index])
                    if status_index is not None
                    else "unknown"
                ),
                source_sheet=f"google:{destination.spreadsheet_id}:{destination.tab_name}",
                source_row=row_number,
            )
        )

    with repository.connect() as connection:
        existing = {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT normalized_url FROM historical_job_links "
                "WHERE client_id=?",
                (profile.client_id,),
            ).fetchall()
        }
    new_records = [record for record in records if record.normalized_url not in existing]
    inserted = 0
    if new_records:
        payload = {
            "client_id": profile.client_id,
            "profile_id": profile.profile_id,
            "destination_id": profile.destination_id,
            "links": [
                {
                    "url": record.normalized_url,
                    "row": record.source_row,
                    "status": record.operator_status,
                }
                for record in new_records
            ],
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        inserted, _ = repository.import_historical_records(
            client_id=profile.client_id,
            workbook_sha256=digest,
            records=new_records,
            importer_version="client-sheet-reconciliation-v1",
        )
    return {
        "sheet_links": len(records),
        "history_inserted": inserted,
        "history_already_present": len(records) - inserted,
    }


def _unresolved(
    repository: SQLiteRepository,
    *,
    client_id: str,
    destination: str,
):
    with repository.connect() as connection:
        row = connection.execute(
            "SELECT batch_id FROM daily_batches "
            "WHERE client_id=? AND destination=? AND status!='delivered' "
            "ORDER BY assembled_at,batch_id LIMIT 1",
            (client_id, destination),
        ).fetchone()
    return DailyBatchStore(repository).get(row[0]) if row else None


def _daily_delivered(
    repository: SQLiteRepository,
    *,
    client_id: str,
    destination: str,
    timezone: str,
    now: datetime,
) -> int:
    zone = ZoneInfo(timezone)
    local_date = now.astimezone(zone).date()
    start = datetime.combine(local_date, time.min, tzinfo=zone).astimezone(UTC)
    end = (datetime.combine(local_date, time.min, tzinfo=zone) + timedelta(days=1)).astimezone(UTC)
    with repository.connect() as connection:
        row = connection.execute(
            "SELECT COUNT(DISTINCT group_id) FROM group_deliveries "
            "WHERE client_id=? AND destination=? AND exported_at>=? AND exported_at<?",
            (client_id, destination, start.isoformat(), end.isoformat()),
        ).fetchone()
    return int(row[0]) if row else 0


def _remaining_quota(
    *,
    repository: SQLiteRepository,
    profile: ClientDeliveryProfile,
    destination: ClientSheetDestination,
    sheet_links: int,
    now: datetime,
) -> tuple[int, int]:
    if profile.quota_scope == "sheet_total":
        progress = sheet_links
    else:
        progress = _daily_delivered(
            repository,
            client_id=profile.client_id,
            destination=destination.logical_uri,
            timezone=profile.timezone,
            now=now,
        )
    return max(0, profile.link_quota - progress), progress


def dispatch_profile(
    *,
    repository: SQLiteRepository,
    profile: ClientDeliveryProfile,
    run_id: str,
    gateway: GoogleSheetsGateway,
    now: datetime | None = None,
) -> dict[str, object]:
    now = now or datetime.now(UTC)
    destination = ClientSheetDestinationStore(repository).get(
        profile.client_id, profile.destination_id
    )
    reconciliation = reconcile_sheet_history(
        repository=repository,
        profile=profile,
        destination=destination,
        gateway=gateway,
    )
    remaining, progress = _remaining_quota(
        repository=repository,
        profile=profile,
        destination=destination,
        sheet_links=reconciliation["sheet_links"],
        now=now,
    )
    base = {
        "client_id": profile.client_id,
        "profile_id": profile.profile_id,
        "destination_id": profile.destination_id,
        "quota_scope": profile.quota_scope,
        "quota": profile.link_quota,
        "progress": progress,
        "remaining": remaining,
        "delivery_mode": profile.delivery_mode,
        "reconciliation": reconciliation,
    }
    if remaining == 0:
        return {**base, "action": "quota_complete"}

    unresolved = _unresolved(
        repository,
        client_id=profile.client_id,
        destination=destination.logical_uri,
    )
    if unresolved is not None:
        if profile.delivery_mode == "review":
            return {
                **base,
                "action": "awaiting_review",
                "batch_id": unresolved.batch_id,
                "selected_count": unresolved.selected_count,
                "batch_status": unresolved.status,
            }
        released = finalize_daily_batch(
            repository=repository,
            batch_id=unresolved.batch_id,
            sheets_gateway=gateway,
        )
        if released.status != "delivered":
            return {
                **base,
                "action": "release_failed",
                "batch_id": released.batch_id,
                "error": released.error,
            }
        reconciliation = reconcile_sheet_history(
            repository=repository,
            profile=profile,
            destination=destination,
            gateway=gateway,
        )
        remaining, progress = _remaining_quota(
            repository=repository,
            profile=profile,
            destination=destination,
            sheet_links=reconciliation["sheet_links"],
            now=now,
        )
        base.update(
            progress=progress,
            remaining=remaining,
            reconciliation=reconciliation,
        )
        if remaining == 0:
            return {**base, "action": "quota_complete_after_release"}

    brief_path = Path(profile.brief_path)
    brief = load_search_brief(brief_path)
    if brief.client_id != profile.client_id:
        raise ValueError("delivery profile client does not match SearchBrief")

    inventory = InventoryRunStore(repository)
    run = inventory.get(run_id)
    if run is None or run.status == "running" or run.completed_at is None:
        raise ValueError("inventory run is not complete")
    if run.status != "success" and profile.delivery_mode == "auto":
        return {
            **base,
            "action": "partial_refresh_blocked",
            "run_id": run_id,
            "inventory_status": run.status,
        }

    retention = repository.prune_stale_inventory(
        retention_hours=72,
        now=now,
    )

    evaluation = evaluate_inventory_run(
        repository=repository,
        run_id=run_id,
        brief=brief,
        evaluated_at=now,
    )
    candidate_ids = inventory.active_job_ids(run_id)
    store = DailyBatchStore(repository)
    evidence_sha = store.evidence_digest(profile.client_id, candidate_ids)
    brief_sha = sha256(brief_path.read_bytes()).hexdigest()
    local_day = now.astimezone(ZoneInfo(profile.timezone)).date().isoformat()
    request = DailyBatchRequest(
        client_id=profile.client_id,
        destination=destination.logical_uri,
        destination_id=destination.destination_id,
        destination_config_sha256=destination.config_sha256,
        idempotency_key=f"{local_day}:{run_id}",
        requested_quota=remaining,
        max_jobs_per_employer_per_batch=brief.delivery_policy.max_jobs_per_employer_per_batch,
        employer_cooldown_days=brief.delivery_policy.employer_cooldown_days,
        max_posting_age_hours=brief.posting_freshness.max_age_hours,
        unknown_posting_age_policy=brief.posting_freshness.unknown_policy,
        freshness_evaluated_at=now,
        evidence_scope_id=f"rolling-inventory:{run_id}",
        evaluation_id=(
            f"rolling-inventory:{run_id}:{profile.client_id}:"
            f"{evaluation.evaluated_at.astimezone(UTC).isoformat()}"
        ),
        candidate_job_ids=candidate_ids,
        evidence_sha256=evidence_sha,
        brief_revision_id=brief_path.stem,
        brief_sha256=brief_sha,
        completeness="complete" if run.status == "success" else "partial",
        source_failures=(
            () if run.status == "success" else ("rolling inventory run was partial",)
        ),
    )
    batch = prepare_daily_batch(repository=repository, request=request)
    if batch.selected_count == 0:
        store.discard_prepared(batch.batch_id)
        return {
            **base,
            "action": "no_new_matches",
            "run_id": run_id,
            "evaluated": len(candidate_ids),
            "retention": retention,
        }

    if profile.delivery_mode == "review":
        return {
            **base,
            "action": "prepared_for_review",
            "run_id": run_id,
            "batch_id": batch.batch_id,
            "selected_count": batch.selected_count,
            "shortfall": batch.shortfall,
            "retention": retention,
        }

    delivered = finalize_daily_batch(
        repository=repository,
        batch_id=batch.batch_id,
        sheets_gateway=gateway,
    )
    return {
        **base,
        "action": "delivered" if delivered.status == "delivered" else "release_failed",
        "run_id": run_id,
        "batch_id": delivered.batch_id,
        "selected_count": delivered.selected_count,
        "shortfall": delivered.shortfall,
        "error": delivered.error,
        "retention": retention,
    }


def dispatch_all(
    *,
    repository: SQLiteRepository,
    run_id: str,
    gateway: GoogleSheetsGateway,
) -> list[dict[str, object]]:
    profiles = ClientDeliveryProfileStore(repository).active()
    results = []
    for profile in profiles:
        try:
            results.append(
                dispatch_profile(
                    repository=repository,
                    profile=profile,
                    run_id=run_id,
                    gateway=gateway,
                )
            )
        except (OSError, ValueError) as error:
            results.append(
                {
                    "client_id": profile.client_id,
                    "profile_id": profile.profile_id,
                    "action": "profile_error",
                    "error": f"{type(error).__name__}: {error}",
                }
            )
    return results


def manage_pending(
    *,
    repository: SQLiteRepository,
    client_id: str,
    profile_id: str,
    action: str,
    gateway: GoogleSheetsGateway,
) -> dict[str, object]:
    profile = ClientDeliveryProfileStore(repository).get(client_id, profile_id)
    destination = ClientSheetDestinationStore(repository).get(
        profile.client_id, profile.destination_id
    )
    batch = _unresolved(
        repository,
        client_id=profile.client_id,
        destination=destination.logical_uri,
    )
    if batch is None:
        return {
            "client_id": client_id,
            "profile_id": profile_id,
            "action": "nothing_pending",
        }
    store = DailyBatchStore(repository)
    if action == "discard":
        result = store.discard_prepared(batch.batch_id)
        return {
            "client_id": client_id,
            "profile_id": profile_id,
            "action": "discarded",
            "batch_id": result.batch_id,
        }
    result = finalize_daily_batch(
        repository=repository,
        batch_id=batch.batch_id,
        sheets_gateway=gateway,
    )
    return {
        "client_id": client_id,
        "profile_id": profile_id,
        "action": "released" if result.status == "delivered" else "release_failed",
        "batch_id": result.batch_id,
        "selected_count": result.selected_count,
        "error": result.error,
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m job_scout.client_dispatcher")
    parser.add_argument("--database", default="jobs.sqlite3")
    commands = parser.add_subparsers(dest="command", required=True)

    dispatch = commands.add_parser("dispatch")
    dispatch.add_argument("--run-id", required=True)

    pending = commands.add_parser("pending")
    pending.add_argument("--client", required=True)
    pending.add_argument("--profile", default="primary")
    pending.add_argument("--action", choices=("release", "discard"), required=True)

    args = parser.parse_args()
    repository = SQLiteRepository(args.database)
    gateway = GoogleSheetsGateway()
    if args.command == "dispatch":
        result = dispatch_all(repository=repository, run_id=args.run_id, gateway=gateway)
    else:
        result = manage_pending(
            repository=repository,
            client_id=args.client,
            profile_id=args.profile,
            action=args.action,
            gateway=gateway,
        )
    print(json.dumps(result, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
