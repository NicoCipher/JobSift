"""Bounded live audit for the frozen Workday CXS cohort.

Run after publishing the implementation:
    python -m validation.workday_production_v1.evaluate --commit <40-character SHA>
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from job_scout.collectors.workday import WorkdayCollector
from job_scout.dedupe.resolver import DEDUPE_VERSION
from job_scout.domain.models import CollectionResult, SourceTarget, WorkdayTargetConfig
from job_scout.matching.matcher import MATCHER_VERSION
from job_scout.orchestration.pipeline import run_pipeline
from job_scout.search_brief import load_search_brief
from job_scout.storage.sqlite import SQLiteRepository

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
CONTRACT = ROOT / "validation/workday_contract_v1"
COHORT = CONTRACT / "cohort.csv"
CONTRACT_FREEZE = CONTRACT / "freeze.json"
BRIEF = ROOT / "config/search_briefs/taiwo_operator_sourcing_v1.json"
EXPECTED_COHORT_SHA256 = "d279f6b7c4d0f76b2ac84c552bf9c6f15b815396e87b7d1865093881c94bb156"
EXPECTED_BRIEF_SHA256 = "7502c14a0dfc8ecdfaca9b2d1ba70a2c02aaec67aaf28bb495a5a7ba76d361f6"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def validation_inputs() -> list[dict[str, str]]:
    """Assert the cohort and immutable matching provenance before any request."""
    assert MATCHER_VERSION == "deterministic-v5"
    assert DEDUPE_VERSION == "dedupe-v1"
    assert sha(BRIEF) == EXPECTED_BRIEF_SHA256
    assert sha(COHORT) == EXPECTED_COHORT_SHA256
    freeze = json.loads(CONTRACT_FREEZE.read_text())
    assert freeze["cohort_size"] == 15
    assert freeze["cohort_csv_sha256"] == EXPECTED_COHORT_SHA256
    with COHORT.open(newline="") as handle:
        cohort = list(csv.DictReader(handle))
    assert len(cohort) == 15
    assert all(
        row.get("host")
        and row.get("tenant")
        and row.get("site")
        and row.get("company_historical_label")
        for row in cohort
    )
    assert len({(row["host"], row["tenant"], row["site"]) for row in cohort}) == 15
    return cohort


class ObservedCollector:
    source = "workday"

    def __init__(self, collector: WorkdayCollector) -> None:
        self.collector = collector
        self.result: CollectionResult | None = None

    def collect(self, target: SourceTarget) -> CollectionResult:
        self.result = self.collector.collect(target)
        return self.result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    parser.add_argument("--delay", type=float, default=0.5)
    args = parser.parse_args()
    if len(args.commit) != 40:
        parser.error("--commit requires the published 40-character commit SHA")
    if args.delay < 0:
        parser.error("--delay must be non-negative")
    cohort = validation_inputs()
    output = HERE / "live"
    output.mkdir(parents=True, exist_ok=False)
    code = [
        "job_scout/collectors/workday.py",
        "job_scout/domain/models.py",
        "job_scout/cli.py",
        "job_scout/matching/matcher.py",
        "job_scout/dedupe/resolver.py",
        "job_scout/storage/sqlite.py",
        "job_scout/orchestration/pipeline.py",
        "validation/workday_production_v1/evaluate.py",
    ]
    manifest = {
        "started_at_utc": datetime.now(UTC).isoformat(),
        "implementation_commit": args.commit,
        "python": sys.executable,
        "matcher_version": MATCHER_VERSION,
        "dedupe_version": DEDUPE_VERSION,
        "brief_sha256": sha(BRIEF),
        "cohort_sha256": sha(COHORT),
        "contract_freeze_sha256": sha(CONTRACT_FREEZE),
        "cohort": cohort,
        "code_sha256": {name: sha(ROOT / name) for name in code},
        "delay_seconds": args.delay,
        "live_calls": True,
    }
    # The target list and code provenance exist on disk before the first request.
    write_json(output / "freeze.json", manifest)
    brief = load_search_brief(BRIEF)
    boards = []
    leads = []
    totals: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    decisions: Counter[str] = Counter()
    for row in cohort:
        assert MATCHER_VERSION == "deterministic-v5" and DEDUPE_VERSION == "dedupe-v1"
        directory = output / row["company_historical_label"].strip().casefold()
        directory.mkdir()
        config = WorkdayTargetConfig(host=row["host"], tenant=row["tenant"], site=row["site"])
        target = SourceTarget(
            board_id=config.board_id, company=row["company_historical_label"], workday=config
        )
        collector = WorkdayCollector(delay=args.delay)
        observer = ObservedCollector(collector)
        repository = SQLiteRepository(directory / "jobs.sqlite3")
        csv_path = directory / "jobs.csv"
        started = datetime.now(UTC).isoformat()
        try:
            summary = run_pipeline(
                collector=observer,
                target=target,
                profile=brief,
                repository=repository,
                csv_path=csv_path,
            )
        finally:
            collector.client.close()
        assert observer.result is not None
        with repository.connect() as connection:
            for decision, count in connection.execute(
                "SELECT decision, COUNT(*) FROM job_matches GROUP BY decision"
            ):
                decisions[decision] += count
        with csv_path.open(newline="") as handle:
            delivered = list(csv.DictReader(handle))
        for lead in delivered:
            leads.append({"board": config.board_id, **lead})
        board = {
            "company": row["company_historical_label"],
            "board": config.board_id,
            "historical_url": row["seed_url"],
            "started_at_utc": started,
            "pipeline": summary.__dict__,
            "collector_counts": collector.last_counts,
            "errors": observer.result.errors,
            "delivered": len(delivered),
        }
        boards.append(board)
        statuses[observer.result.status.value] += 1
        totals["normalized"] += len(observer.result.jobs)
        totals["errors"] += len(observer.result.errors)
        totals["delivered"] += len(delivered)
    write_json(output / "boards.json", boards)
    write_json(output / "delivered_leads.json", leads)
    write_json(
        output / "summary.json",
        {
            "completed_at_utc": datetime.now(UTC).isoformat(),
            "totals": dict(totals),
            "statuses": dict(statuses),
            "decisions": dict(decisions),
            "boards": len(boards),
            "nvidia": next(board for board in boards if board["company"].casefold() == "nvidia"),
        },
    )


if __name__ == "__main__":
    main()
