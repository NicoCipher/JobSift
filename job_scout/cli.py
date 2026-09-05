from __future__ import annotations

import argparse
import json
from pathlib import Path

from job_scout.collectors.ashby import AshbyCollector
from job_scout.collectors.greenhouse import GreenhouseCollector
from job_scout.collectors.workday import WorkdayCollector
from job_scout.domain.models import SourceTarget, WorkdayTargetConfig
from job_scout.orchestration.pipeline import run_pipeline
from job_scout.search_brief import create_search_brief_interactively, load_search_brief
from job_scout.storage.sqlite import SQLiteRepository


def main() -> None:
    parser = argparse.ArgumentParser(prog="job-scout")
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect")
    collect.add_argument(
        "--source", choices=("greenhouse", "ashby", "workday"), default="greenhouse"
    )
    collect.add_argument("--client", required=True, help="Path to client JSON config")
    collect.add_argument("--board")
    collect.add_argument("--company", required=True)
    collect.add_argument("--workday-host")
    collect.add_argument("--workday-tenant")
    collect.add_argument("--workday-site")
    collect.add_argument("--database", default="jobs.sqlite3")
    collect.add_argument("--csv", default="exports/jobs.csv")
    profile = commands.add_parser("profile")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    profile_create = profile_commands.add_parser("create")
    profile_create.add_argument("--output-dir", default="config/search_briefs")
    args = parser.parse_args()
    if args.command == "profile":
        create_search_brief_interactively(output_dir=args.output_dir)
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
        }[args.source](),
        target=target,
        profile=profile,
        repository=SQLiteRepository(args.database),
        csv_path=args.csv,
    )
    print(json.dumps(summary.__dict__, sort_keys=True))


if __name__ == "__main__":
    main()
