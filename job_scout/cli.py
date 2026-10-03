from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import Counter
from pathlib import Path

from job_scout.collectors.ashby import AshbyCollector
from job_scout.collectors.greenhouse import GreenhouseCollector
from job_scout.collectors.lever import LeverCollector
from job_scout.collectors.smartrecruiters import SmartRecruitersCollector
from job_scout.collectors.workday import WorkdayCollector
from job_scout.delivery_destinations import (
    DELIVERY_FIELDS,
    ClientSheetDestinationStore,
    ClientSheetRegistrationRequest,
)
from job_scout.delivery_profiles import (
    ClientDeliveryProfileStore,
    delivery_profile_control_id,
)
from job_scout.domain.models import LeverTargetConfig, SourceTarget, WorkdayTargetConfig
from job_scout.export.batch_sheets import GoogleSheetsGateway
from job_scout.history import explicit_blacklist_evidence, historical_records, workbook_sha256
from job_scout.orchestration.daily_batch import finalize_daily_batch
from job_scout.orchestration.pipeline import run_pipeline
from job_scout.search_brief import create_search_brief_interactively, load_search_brief
from job_scout.sourcing_plan import load_sourcing_plan, run_sourcing_plan
from job_scout.storage.daily_batches import DailyBatchStore
from job_scout.storage.sqlite import SQLiteRepository


SERIALIZED_PROFILE_MUTATION_WORKFLOWS = frozenset(
    {
        "Client Delivery Control",
        "Configure Client Delivery Profile",
    }
)


def _require_serialized_profile_mutation(repository, command: str) -> None:
    """Reject Turso-backed profile mutations outside the queued production workflows."""
    if not repository.remote_url:
        return
    if (
        os.getenv("GITHUB_ACTIONS", "").casefold() != "true"
        or os.getenv("GITHUB_WORKFLOW", "") not in SERIALIZED_PROFILE_MUTATION_WORKFLOWS
    ):
        raise RuntimeError(
            "Turso-backed delivery-profile mutations must run through "
            "Client Delivery Control or Configure Client Delivery Profile"
        )


def main() -> None:
    parser = argparse.ArgumentParser(prog="job-scout")
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect")
    collect.add_argument(
        "--source", choices=("greenhouse", "ashby", "workday", "lever", "smartrecruiters"), default="greenhouse"
    )
    collect.add_argument("--client", required=True, help="Path to client JSON config")
    collect.add_argument("--board")
    collect.add_argument("--company", required=True)
    collect.add_argument("--workday-host")
    collect.add_argument("--workday-tenant")
    collect.add_argument("--workday-site")
    collect.add_argument("--lever-instance", choices=("global", "eu"))
    collect.add_argument("--database", default="jobs.sqlite3")
    collect.add_argument("--csv", default="exports/jobs.csv")
    source_plan = commands.add_parser("source", help="Run a multi-target sourcing plan")
    source_plan.add_argument("--plan", required=True, help="Path to sourcing-plan JSON")
    source_plan.add_argument("--reports-dir", default="runs")
    history = commands.add_parser("history", help="Import historical operator job-link evidence")
    history_commands = history.add_subparsers(dest="history_command", required=True)
    history_import = history_commands.add_parser("import")
    history_import.add_argument(
        "--client", required=True, help="Client identifier to scope suppression"
    )
    history_import.add_argument("--workbook", required=True, type=Path)
    history_import.add_argument("--database", default="jobs.sqlite3")
    destination = commands.add_parser(
        "destination", help="Register and manage client-owned delivery destinations"
    )
    destination_commands = destination.add_subparsers(
        dest="destination_command", required=True
    )
    destination_register = destination_commands.add_parser(
        "register-google-sheet",
        help="Validate and register a client-owned Google Sheet",
    )
    destination_register.add_argument("--database", default="jobs.sqlite3")
    destination_register.add_argument("--client", required=True)
    destination_register.add_argument("--destination-id", required=True)
    destination_register.add_argument("--display-name", required=True)
    destination_register.add_argument("--spreadsheet", required=True)
    destination_register.add_argument("--tab", required=True)
    destination_register.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="JOBSIFT_FIELD=CLIENT_HEADER",
        help=(
            "Repeat for every delivered field. Supported fields: "
            + ", ".join(DELIVERY_FIELDS)
        ),
    )
    destination_register.add_argument(
        "--mapping-json",
        help="JSON object mapping JobSift fields to client header names",
    )
    destination_register_file = destination_commands.add_parser(
        "register-google-sheet-file",
        help="Register a client-owned Google Sheet from a private JSON file",
    )
    destination_register_file.add_argument("--database", default="jobs.sqlite3")
    destination_register_file.add_argument(
        "--registration-file",
        required=True,
        type=Path,
        help="Private JSON file containing the client Sheet registration payload",
    )
    destination_list = destination_commands.add_parser("list")
    destination_list.add_argument("--database", default="jobs.sqlite3")
    destination_list.add_argument("--client", required=True)
    destination_disable = destination_commands.add_parser("disable")
    destination_disable.add_argument("--database", default="jobs.sqlite3")
    destination_disable.add_argument("--client", required=True)
    destination_disable.add_argument("--destination-id", required=True)

    delivery_profile = commands.add_parser(
        "delivery-profile",
        help="Manage persistent per-client Sheet quota and delivery controls",
    )
    delivery_profile_commands = delivery_profile.add_subparsers(
        dest="delivery_profile_command", required=True
    )
    delivery_profile_set = delivery_profile_commands.add_parser("set")
    delivery_profile_set.add_argument("--database", default="jobs.sqlite3")
    delivery_profile_set.add_argument("--client", required=True)
    delivery_profile_set.add_argument("--destination-id", required=True)
    delivery_profile_set.add_argument(
        "--plan",
        required=True,
        type=Path,
        help="Registered sourcing-plan JSON for this client",
    )
    delivery_profile_set.add_argument("--daily-quota", required=True, type=int)
    delivery_profile_set.add_argument(
        "--status", choices=("active", "paused"), default="paused"
    )
    delivery_profile_set.add_argument(
        "--delivery-mode", choices=("review", "auto"), default="review"
    )
    delivery_profile_set.add_argument("--timezone", default="Africa/Lagos")

    delivery_profile_private = delivery_profile_commands.add_parser(
        "set-from-registration",
        help="Create/update a profile from a private Sheet-registration JSON file",
    )
    delivery_profile_private.add_argument("--database", default="jobs.sqlite3")
    delivery_profile_private.add_argument("--registration-file", required=True, type=Path)
    delivery_profile_private.add_argument("--plan", required=True, type=Path)
    delivery_profile_private.add_argument("--daily-quota", required=True, type=int)
    delivery_profile_private.add_argument(
        "--status", choices=("active", "paused"), default="paused"
    )
    delivery_profile_private.add_argument(
        "--delivery-mode", choices=("review", "auto"), default="review"
    )
    delivery_profile_private.add_argument("--timezone", default="Africa/Lagos")
    delivery_profile_private.add_argument("--reconcile", action="store_true")

    delivery_profile_list = delivery_profile_commands.add_parser("list")
    delivery_profile_list.add_argument("--database", default="jobs.sqlite3")

    delivery_profile_status = delivery_profile_commands.add_parser("status")
    delivery_profile_status.add_argument("--database", default="jobs.sqlite3")
    delivery_profile_status.add_argument("--profile-id", required=True)
    delivery_profile_status.add_argument("--reconcile", action="store_true")

    for name in ("pause", "resume"):
        command = delivery_profile_commands.add_parser(name)
        command.add_argument("--database", default="jobs.sqlite3")
        command.add_argument("--profile-id", required=True)

    delivery_profile_quota = delivery_profile_commands.add_parser("set-quota")
    delivery_profile_quota.add_argument("--database", default="jobs.sqlite3")
    delivery_profile_quota.add_argument("--profile-id", required=True)
    delivery_profile_quota.add_argument("--daily-quota", required=True, type=int)

    delivery_profile_mode = delivery_profile_commands.add_parser("set-mode")
    delivery_profile_mode.add_argument("--database", default="jobs.sqlite3")
    delivery_profile_mode.add_argument("--profile-id", required=True)
    delivery_profile_mode.add_argument(
        "--delivery-mode", choices=("review", "auto"), required=True
    )

    delivery_profile_timezone = delivery_profile_commands.add_parser("set-timezone")
    delivery_profile_timezone.add_argument("--database", default="jobs.sqlite3")
    delivery_profile_timezone.add_argument("--profile-id", required=True)
    delivery_profile_timezone.add_argument("--timezone", required=True)

    for name in ("release-batch", "discard-batch"):
        command = delivery_profile_commands.add_parser(name)
        command.add_argument("--database", default="jobs.sqlite3")
        command.add_argument("--profile-id", required=True)
        command.add_argument("--batch-id", required=True)
        if name == "release-batch":
            command.add_argument(
                "--confirm-batch-id",
                required=True,
                help="Repeat the reviewed batch ID to authorize Sheet delivery",
            )

    profile = commands.add_parser("profile")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    profile_create = profile_commands.add_parser("create")
    profile_create.add_argument("--output-dir", default="config/search_briefs")
    batch = commands.add_parser("batch", help="Review or release an already prepared daily batch")
    batch_commands = batch.add_subparsers(dest="batch_command", required=True)
    for name in ("review", "release"):
        command = batch_commands.add_parser(name)
        command.add_argument("--database", default="jobs.sqlite3")
        command.add_argument("--batch-id", required=True)
        if name == "release":
            command.add_argument(
                "--confirm-batch-id",
                required=True,
                help="Repeat the reviewed batch ID to authorize delivery",
            )
    args = parser.parse_args()
    if args.command == "destination":
        from job_scout.domain.daily_batch import BatchConflict

        repository = SQLiteRepository(args.database)
        store = ClientSheetDestinationStore(repository)
        try:
            if args.destination_command == "register-google-sheet":
                if args.map and args.mapping_json:
                    parser.error("use either --map or --mapping-json, not both")
                mapping: dict[str, str] = {}
                if args.mapping_json:
                    try:
                        raw_mapping = json.loads(args.mapping_json)
                    except json.JSONDecodeError as exc:
                        parser.error(f"invalid --mapping-json: {exc.msg}")
                    if not isinstance(raw_mapping, dict) or not all(
                        isinstance(source, str) and isinstance(target, str)
                        for source, target in raw_mapping.items()
                    ):
                        parser.error("--mapping-json must be an object of string-to-string values")
                    mapping = {
                        source.strip(): target.strip()
                        for source, target in raw_mapping.items()
                    }
                else:
                    for value in args.map:
                        if "=" not in value:
                            parser.error("--map must use JOBSIFT_FIELD=CLIENT_HEADER")
                        source, target = (part.strip() for part in value.split("=", 1))
                        if source in mapping:
                            parser.error(f"duplicate JobSift field in --map: {source}")
                        mapping[source] = target
                for source, target in mapping.items():
                    if source not in DELIVERY_FIELDS:
                        parser.error(f"unsupported JobSift field in mapping: {source}")
                    if not target:
                        parser.error("client header in mapping must not be blank")
                result = store.register_google_sheet(
                    client_id=args.client,
                    destination_id=args.destination_id,
                    display_name=args.display_name,
                    spreadsheet=args.spreadsheet,
                    tab_name=args.tab,
                    column_mapping=mapping,
                    gateway=GoogleSheetsGateway(),
                )
                print(
                    json.dumps(
                        {
                            "client_id": result.client_id,
                            "destination_id": result.destination_id,
                            "display_name": result.display_name,
                            "logical_destination": result.logical_uri,
                            "spreadsheet_id": result.spreadsheet_id,
                            "sheet_id": result.sheet_id,
                            "tab_name": result.tab_name,
                            "header": result.header,
                            "column_mapping": result.column_mapping,
                            "header_sha256": result.header_sha256,
                            "config_sha256": result.config_sha256,
                            "status": result.status,
                        },
                        sort_keys=True,
                    )
                )
            elif args.destination_command == "register-google-sheet-file":
                try:
                    registration = ClientSheetRegistrationRequest.model_validate_json(
                        args.registration_file.read_text(encoding="utf-8")
                    )
                except OSError as exc:
                    parser.error(f"unable to read registration file: {exc}")
                for source in registration.column_mapping:
                    if source not in DELIVERY_FIELDS:
                        parser.error(f"unsupported JobSift field in mapping: {source}")
                result = store.register_google_sheet(
                    client_id=registration.client_id,
                    destination_id=registration.destination_id,
                    display_name=registration.display_name,
                    spreadsheet=registration.spreadsheet,
                    tab_name=registration.tab,
                    column_mapping=registration.column_mapping,
                    gateway=GoogleSheetsGateway(),
                )
                print(
                    json.dumps(
                        {
                            "client_id": result.client_id,
                            "destination_id": result.destination_id,
                            "logical_destination": result.logical_uri,
                            "status": result.status,
                        },
                        sort_keys=True,
                    )
                )
            elif args.destination_command == "list":
                values = store.list(args.client)
                print(
                    json.dumps(
                        [
                            {
                                "destination_id": value.destination_id,
                                "display_name": value.display_name,
                                "logical_destination": value.logical_uri,
                                "spreadsheet_id": value.spreadsheet_id,
                                "sheet_id": value.sheet_id,
                                "tab_name": value.tab_name,
                                "column_mapping": value.column_mapping,
                                "status": value.status,
                                "config_sha256": value.config_sha256,
                            }
                            for value in values
                        ],
                        sort_keys=True,
                    )
                )
            else:
                result = store.disable(args.client, args.destination_id)
                print(
                    json.dumps(
                        {
                            "client_id": result.client_id,
                            "destination_id": result.destination_id,
                            "status": result.status,
                        },
                        sort_keys=True,
                    )
                )
        except (BatchConflict, OSError, ValueError, sqlite3.Error) as exc:
            parser.error(f"unable to manage destination: {exc}")
        return
    if args.command == "delivery-profile":
        from job_scout.domain.daily_batch import BatchConflict

        def _profile_for_control_id():
            return store.get_by_control_id(args.profile_id)

        def _public(value):
            return store.public_status(value)

        def _set_profile(client_id, destination_id, plan_path):
            plan_path = plan_path.resolve()
            plan = load_sourcing_plan(plan_path)
            brief_path = Path(plan.search_brief)
            if not brief_path.is_absolute():
                brief_path = (plan_path.parent / brief_path).resolve()
            brief = load_search_brief(brief_path)
            if brief.client_id != client_id:
                parser.error(
                    "sourcing plan SearchBrief client_id does not match the client"
                )
            return store.upsert(
                client_id=client_id,
                destination_id=destination_id,
                sourcing_plan_id=plan.plan_id,
                daily_quota=args.daily_quota,
                status=args.status,
                delivery_mode=args.delivery_mode,
                timezone=args.timezone,
            )

        try:
            repository = SQLiteRepository(args.database)
            store = ClientDeliveryProfileStore(repository)
            command = args.delivery_profile_command
            mutating_profile_command = (
                command not in {"list", "status"}
                or (command == "status" and args.reconcile)
            )
            if mutating_profile_command:
                _require_serialized_profile_mutation(repository, command)
            if command == "set":
                value = _set_profile(args.client, args.destination_id, args.plan)
                print(json.dumps(_public(value), sort_keys=True))
            elif command == "set-from-registration":
                registration = ClientSheetRegistrationRequest.model_validate_json(
                    args.registration_file.read_text(encoding="utf-8")
                )
                value = _set_profile(
                    registration.client_id,
                    registration.destination_id,
                    args.plan,
                )
                if args.reconcile:
                    store.reconcile_destination_sheet(
                        value, gateway=GoogleSheetsGateway()
                    )
                print(json.dumps(_public(value), sort_keys=True))
            elif command == "list":
                print(
                    json.dumps(
                        [_public(value) for value in store.list()],
                        sort_keys=True,
                    )
                )
            elif command == "status":
                value = _profile_for_control_id()
                if args.reconcile:
                    store.reconcile_destination_sheet(
                        value, gateway=GoogleSheetsGateway()
                    )
                print(json.dumps(_public(value), sort_keys=True))
            elif command in {"pause", "resume", "set-quota", "set-mode", "set-timezone"}:
                current = _profile_for_control_id()
                changes = {}
                if command == "pause":
                    changes["status"] = "paused"
                elif command == "resume":
                    changes["status"] = "active"
                elif command == "set-quota":
                    changes["daily_quota"] = args.daily_quota
                elif command == "set-mode":
                    changes["delivery_mode"] = args.delivery_mode
                else:
                    changes["timezone"] = args.timezone
                value = store.update_controls(current, **changes)
                print(json.dumps(_public(value), sort_keys=True))
            else:
                profile = _profile_for_control_id()
                batch_store = DailyBatchStore(repository)
                if args.delivery_profile_command == "release-batch":
                    if args.confirm_batch_id != args.batch_id:
                        parser.error("confirmation must match the reviewed batch ID")
                    result, reconciliation, _remaining = store.guard_batch_release(
                        profile,
                        args.batch_id,
                        gateway=GoogleSheetsGateway(),
                    )
                    result = finalize_daily_batch(
                        repository=repository,
                        batch_id=result.batch_id,
                        expected_generation_id=result.generation_id,
                    )
                else:
                    result = batch_store.get(args.batch_id)
                    expected_control_id = delivery_profile_control_id(
                        result.request.client_id,
                        result.request.destination_id or "",
                    )
                    if expected_control_id != args.profile_id.casefold():
                        parser.error(
                            "batch does not belong to the selected delivery profile"
                        )
                    reconciliation = None
                    result = batch_store.discard_prepared(
                        result.batch_id,
                        expected_generation_id=result.generation_id,
                    )
                payload = {
                    "profile_id": delivery_profile_control_id(
                        profile.client_id, profile.destination_id
                    ),
                    "batch_id": result.batch_id,
                    "status": result.status,
                    "selected_count": result.selected_count,
                    "shortfall": result.shortfall,
                    "error": result.error,
                }
                if reconciliation is not None:
                    payload["sheet_reconciliation"] = reconciliation
                print(json.dumps(payload, sort_keys=True))
                if (
                    args.delivery_profile_command == "release-batch"
                    and result.status != "delivered"
                ):
                    parser.exit(1)
        except (
            BatchConflict,
            OSError,
            RuntimeError,
            ValueError,
            sqlite3.Error,
        ) as exc:
            parser.error(f"unable to manage delivery profile: {exc}")
        return

    if args.command == "batch":
        from job_scout.domain.daily_batch import BatchConflict

        if args.batch_command == "release" and args.confirm_batch_id != args.batch_id:
            parser.error("confirmation must match the reviewed batch ID")
        try:
            if args.batch_command == "review":
                result, rows = DailyBatchStore.review_readonly(args.database, args.batch_id)
            else:
                repository = SQLiteRepository(args.database)
                store = DailyBatchStore(repository)
                result = store.get(args.batch_id)
                if result.request.destination_id is not None:
                    profiles = ClientDeliveryProfileStore(repository).list(
                        result.request.client_id
                    )
                    if any(
                        profile.destination_id == result.request.destination_id
                        for profile in profiles
                    ):
                        raise BatchConflict(
                            "profile-attributed client Sheet batches must be released "
                            "through delivery-profile release-batch"
                        )
                result = finalize_daily_batch(
                    repository=repository,
                    batch_id=result.batch_id,
                    expected_generation_id=result.generation_id,
                )
                rows = store.export_rows(result.batch_id)
            print(
                json.dumps(
                    {
                        "batch_id": result.batch_id,
                        "client_id": result.request.client_id,
                        "destination": result.request.destination,
                        "status": result.status,
                        "requested_quota": result.request.requested_quota,
                        "selected_count": result.selected_count,
                        "shortfall": result.shortfall,
                        "completeness": result.request.completeness,
                        "source_failures": result.request.source_failures,
                        "error": result.error,
                        "rows": rows,
                    },
                    sort_keys=True,
                )
            )
            if args.batch_command == "release" and result.status != "delivered":
                parser.exit(1)
        except (BatchConflict, OSError, ValueError, sqlite3.Error) as exc:
            parser.error(f"unable to {args.batch_command} batch: {exc}")
        return
    if args.command == "profile":
        create_search_brief_interactively(output_dir=args.output_dir)
        return
    if args.command == "source":
        plan_path = Path(args.plan).resolve()
        try:
            plan = load_sourcing_plan(plan_path)
            report = run_sourcing_plan(
                plan,
                base_dir=plan_path.parent,
                reports_dir=Path(args.reports_dir).resolve(),
            )
        except (OSError, ValueError) as exc:
            parser.error(f"unable to run sourcing plan: {exc}")
        print(json.dumps(report.model_dump(mode="json"), sort_keys=True))
        return
    if args.command == "history":
        try:
            records = historical_records(args.workbook)
            blacklist = explicit_blacklist_evidence(args.workbook)
            checksum = workbook_sha256(args.workbook)
            client_id = (
                load_search_brief(Path(args.client)).client_id
                if Path(args.client).is_file()
                else args.client
            )
            inserted, already_present = SQLiteRepository(args.database).import_historical_records(
                client_id=client_id,
                workbook_sha256=checksum,
                records=records,
                blacklist_evidence=blacklist,
            )
        except (OSError, ValueError) as exc:
            parser.error(f"unable to import historical operator state: {exc}")
        statuses = Counter(record.operator_status for record in records)
        supported = sum(record.source_job_id is not None for record in records)
        malformed = sum(
            not record.normalized_url.startswith(("http://", "https://")) for record in records
        )
        print(
            json.dumps(
                {
                    "already_present": already_present,
                    "applied": statuses["applied"],
                    "client_id": client_id,
                    "explicit_blacklist_evidence": len(blacklist),
                    "inserted": inserted,
                    "malformed_or_unusable": malformed,
                    "not_applied": statuses["not_applied"],
                    "supported_source_identities": supported,
                    "total_rows": len(records),
                    "unknown": statuses["unknown"],
                    "url_only_records": len(records) - supported,
                    "workbook_sha256": checksum,
                },
                sort_keys=True,
            )
        )
        return
    if args.source == "workday":
        if not all((args.workday_host, args.workday_tenant, args.workday_site)):
            parser.error("Workday requires --workday-host, --workday-tenant, and --workday-site")
        workday = WorkdayTargetConfig(
            host=args.workday_host,
            tenant=args.workday_tenant,
            site=args.workday_site,
        )
        target = SourceTarget(board_id=workday.board_id, company=args.company, workday=workday)
    elif args.source == "lever":
        if not args.board or not args.lever_instance:
            parser.error("Lever requires --board and --lever-instance")
        lever = LeverTargetConfig(instance=args.lever_instance, site=args.board)
        target = SourceTarget(board_id=lever.board_id, company=args.company, lever=lever)
    else:
        if not args.board:
            parser.error("--board is required for Greenhouse, Ashby, and SmartRecruiters")
        target = SourceTarget(board_id=args.board, company=args.company)
    profile = load_search_brief(Path(args.client))
    summary = run_pipeline(
        collector={
            "greenhouse": GreenhouseCollector,
            "ashby": AshbyCollector,
            "workday": WorkdayCollector,
            "lever": LeverCollector,
            "smartrecruiters": SmartRecruitersCollector,
        }[args.source](),
        target=target,
        profile=profile,
        repository=SQLiteRepository(args.database),
        csv_path=args.csv,
    )
    print(json.dumps(summary.__dict__, sort_keys=True))


if __name__ == "__main__":
    main()
