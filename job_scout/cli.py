from __future__ import annotations

import argparse
import json
from pathlib import Path

from job_scout.collectors.greenhouse import GreenhouseCollector
from job_scout.domain.models import CandidateProfile, SourceTarget
from job_scout.orchestration.pipeline import run_pipeline
from job_scout.storage.sqlite import SQLiteRepository


def main() -> None:
    parser = argparse.ArgumentParser(prog="job-scout")
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect")
    collect.add_argument("--client", required=True, help="Path to client JSON config")
    collect.add_argument("--board", required=True)
    collect.add_argument("--company", required=True)
    collect.add_argument("--database", default="jobs.sqlite3")
    collect.add_argument("--csv", default="exports/jobs.csv")
    args = parser.parse_args()
    profile = CandidateProfile.model_validate_json(Path(args.client).read_text(encoding="utf-8"))
    summary = run_pipeline(
        collector=GreenhouseCollector(),
        target=SourceTarget(board_id=args.board, company=args.company),
        profile=profile,
        repository=SQLiteRepository(args.database),
        csv_path=args.csv,
    )
    print(json.dumps(summary.__dict__, sort_keys=True))


if __name__ == "__main__":
    main()
