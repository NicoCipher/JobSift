from __future__ import annotations

import argparse
import json
from pathlib import Path

from job_scout.collectors.ashby import AshbyCollector
from job_scout.collectors.greenhouse import GreenhouseCollector
from job_scout.domain.models import SourceTarget
from job_scout.orchestration.pipeline import run_pipeline
from job_scout.search_brief import create_search_brief_interactively, load_search_brief
from job_scout.storage.sqlite import SQLiteRepository


def main() -> None:
    parser = argparse.ArgumentParser(prog="job-scout")
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect")
    collect.add_argument("--source", choices=("greenhouse", "ashby"), default="greenhouse")
    collect.add_argument("--client", required=True, help="Path to client JSON config")
    collect.add_argument("--board", required=True)
    collect.add_argument("--company", required=True)
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
    profile = load_search_brief(Path(args.client))
    summary = run_pipeline(
        collector={"greenhouse": GreenhouseCollector, "ashby": AshbyCollector}[args.source](),
        target=SourceTarget(board_id=args.board, company=args.company),
        profile=profile,
        repository=SQLiteRepository(args.database),
        csv_path=args.csv,
    )
    print(json.dumps(summary.__dict__, sort_keys=True))


if __name__ == "__main__":
    main()
