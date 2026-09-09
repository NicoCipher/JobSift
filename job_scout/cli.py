from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from job_scout.collectors.ashby import AshbyCollector
from job_scout.collectors.greenhouse import GreenhouseCollector
from job_scout.collectors.lever import LeverCollector
from job_scout.collectors.workday import WorkdayCollector
from job_scout.domain.models import LeverTargetConfig, SourceTarget, WorkdayTargetConfig
from job_scout.history import explicit_blacklist_evidence, historical_records, workbook_sha256
from job_scout.orchestration.pipeline import run_pipeline
from job_scout.search_brief import create_search_brief_interactively, load_search_brief
from job_scout.sourcing_plan import load_sourcing_plan, run_sourcing_plan
from job_scout.storage.sqlite import SQLiteRepository


def main() -> None:
    parser = argparse.ArgumentParser(prog="job-scout")
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect")
    collect.add_argument(
        "--source", choices=("greenhouse", "ashby", "workday", "lever"), default="greenhouse"
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
    profile = commands.add_parser("profile")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    profile_create = profile_commands.add_parser("create")
    profile_create.add_argument("--output-dir", default="config/search_briefs")
    args = parser.parse_args()
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
            parser.error("--board is required for Greenhouse and Ashby")
        target = SourceTarget(board_id=args.board, company=args.company)
    profile = load_search_brief(Path(args.client))
    summary = run_pipeline(
        collector={
            "greenhouse": GreenhouseCollector,
            "ashby": AshbyCollector,
            "workday": WorkdayCollector,
            "lever": LeverCollector,
        }[args.source](),
        target=target,
        profile=profile,
        repository=SQLiteRepository(args.database),
        csv_path=args.csv,
    )
    print(json.dumps(summary.__dict__, sort_keys=True))


if __name__ == "__main__":
    main()
