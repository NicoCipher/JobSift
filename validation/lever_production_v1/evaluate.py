"""Bounded live validation for the frozen Lever production cohort.

Run with:
    python -m validation.lever_production_v1.evaluate --commit <40-character SHA>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from job_scout.collectors.lever import LeverCollector
from job_scout.dedupe.resolver import DEDUPE_VERSION
from job_scout.domain.models import CollectionResult, Job, LeverTargetConfig, SourceTarget
from job_scout.matching.matcher import MATCHER_VERSION
from job_scout.orchestration.pipeline import PipelineSummary, run_pipeline
from job_scout.search_brief import load_search_brief
from job_scout.storage.sqlite import SQLiteRepository

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
CONTRACT_COHORT = ROOT / "validation/lever_contract_v1/cohort.json"
BRIEF = ROOT / "config/search_briefs/taiwo_operator_sourcing_v1.json"
EXPECTED_MANIFEST = "e44167fefc5e1983cba8561f160707cbff0b70a47897bc4e7194f66ff59767a8"
EXPECTED_BRIEF_SHA256 = "7502c14a0dfc8ecdfaca9b2d1ba70a2c02aaec67aaf28bb495a5a7ba76d361f6"
EXPECTED_TARGETS = (
    ("global", "crosslaketech"),
    ("global", "pushpress"),
    ("global", "voleon"),
    ("global", "magnetforensics"),
    ("global", "aledade"),
    ("global", "AviveSolutions"),
    ("global", "filevine"),
    ("global", "patchmypc"),
    ("eu", "blackshark"),
    ("eu", "coinspaid"),
    ("eu", "eneba"),
    ("eu", "xm"),
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def validation_inputs() -> list[dict[str, object]]:
    """Verify immutable cohort and matching provenance before live requests."""
    assert MATCHER_VERSION == "deterministic-v5"
    assert DEDUPE_VERSION == "dedupe-v1"
    assert sha(BRIEF) == EXPECTED_BRIEF_SHA256
    cohort = json.loads(CONTRACT_COHORT.read_text(encoding="utf-8"))
    assert cohort["manifest_sha256"] == EXPECTED_MANIFEST
    targets = cohort["targets"]
    assert isinstance(targets, list)
    assert tuple((target["instance"], target["site"]) for target in targets) == EXPECTED_TARGETS
    return targets


class ObservedCollector:
    """Expose the real collector result after generic pipeline execution."""

    source = LeverCollector.source

    def __init__(self, collector: LeverCollector) -> None:
        self.collector = collector
        self.result: CollectionResult | None = None

    def collect(self, target: SourceTarget) -> CollectionResult:
        self.result = self.collector.collect(target)
        return self.result


def source_target(row: dict[str, object]) -> SourceTarget:
    config = LeverTargetConfig(instance=str(row["instance"]), site=str(row["site"]))  # type: ignore[arg-type]
    return SourceTarget(
        board_id=config.board_id,
        company=str(row["historical_company_string"]),
        lever=config,
    )


def job_key(job: Job) -> tuple[str, str, str]:
    return str(job.raw_metadata["instance"]), str(job.raw_metadata["site"]), job.source_job_id


def job_metrics(result: CollectionResult, collector: LeverCollector) -> dict[str, object]:
    jobs = result.jobs
    return {
        "status": result.status.value,
        "raw_collector_error_count": len(result.errors),
        "normalized_jobs": len(jobs),
        "quarantined_postings": int(collector.last_counts["quarantined"]),
        "pages": int(collector.last_counts["pages"]),
        "received_postings": int(collector.last_counts["received"]),
        "unique_job_ids": len({job.id for job in jobs}),
        "unique_source_job_ids": len({job.source_job_id for job in jobs}),
        "source_board_ids": sorted({job.source_board_id for job in jobs}),
        "hosted_url_hosts": dict(
            sorted(Counter(urlsplit(str(job.job_url)).hostname for job in jobs).items())
        ),
        "apply_url_hosts": dict(
            sorted(
                Counter(
                    urlsplit(str(job.apply_url)).hostname for job in jobs if job.apply_url
                ).items()
            )
        ),
        "remote_statuses": dict(sorted(Counter(job.remote_status.value for job in jobs).items())),
        "eligible_countries": dict(
            sorted(Counter(country for job in jobs for country in job.eligible_countries).items())
        ),
        "country_unknown": sum(not job.eligible_countries for job in jobs),
        "employment_types": dict(
            sorted(
                Counter(
                    job.employment_type.value if job.employment_type else "unknown" for job in jobs
                ).items()
            )
        ),
        "missing_description": sum(job.description_text is None for job in jobs),
        "missing_department": sum(job.department is None for job in jobs),
        "error_examples": result.errors[:10],
    }


def collect_once(
    row: dict[str, object], *, delay: float
) -> tuple[CollectionResult, LeverCollector, dict[str, object]]:
    target = source_target(row)
    collector = LeverCollector(delay=delay)
    try:
        result = collector.collect(target)
    finally:
        collector.client.close()
    return result, collector, job_metrics(result, collector)


def verify_identity(jobs: list[Job]) -> dict[str, object]:
    by_id: Counter[str] = Counter(job.id for job in jobs)
    by_target_source_id: Counter[tuple[str, str, str]] = Counter(job_key(job) for job in jobs)
    incorrect: list[str] = []
    for job in jobs:
        instance, site, provider_id = job_key(job)
        expected = str(uuid.uuid5(uuid.NAMESPACE_URL, f"lever:{instance}:{site}:{provider_id}"))
        if (
            job.source != "lever"
            or job.source_board_id != f"{instance}:{site}"
            or job.id != expected
        ):
            incorrect.append(job.id)
    return {
        "jobs_checked": len(jobs),
        "duplicate_job_ids": sorted(identifier for identifier, count in by_id.items() if count > 1),
        "duplicate_source_ids_within_target": [
            {"instance": instance, "site": site, "source_job_id": source_id}
            for (instance, site, source_id), count in by_target_source_id.items()
            if count > 1
        ],
        "invalid_identity_records": sorted(incorrect),
    }


def repeat_stability(first: list[Job], second: list[Job]) -> dict[str, object]:
    first_by_key = {job_key(job): job for job in first}
    second_by_key = {job_key(job): job for job in second}
    common = sorted(first_by_key.keys() & second_by_key.keys())
    unstable: list[dict[str, str]] = []
    content_changed: list[dict[str, str]] = []
    for key in common:
        earlier, later = first_by_key[key], second_by_key[key]
        if (
            earlier.id != later.id
            or earlier.source_board_id != later.source_board_id
            or earlier.source_job_id != later.source_job_id
            or earlier.canonical_url != later.canonical_url
        ):
            unstable.append({"instance": key[0], "site": key[1], "source_job_id": key[2]})
        elif earlier.content_fingerprint != later.content_fingerprint:
            content_changed.append({"instance": key[0], "site": key[1], "source_job_id": key[2]})
    return {
        "first_only_provider_postings": len(first_by_key.keys() - second_by_key.keys()),
        "second_only_provider_postings": len(second_by_key.keys() - first_by_key.keys()),
        "postings_in_both_runs": len(common),
        "identity_or_url_instability": unstable,
        "provider_content_changes": content_changed,
    }


def pipeline_metrics(
    targets: list[dict[str, object]], *, delay: float
) -> tuple[dict[str, object], list[dict[str, object]]]:
    brief = load_search_brief(BRIEF)
    pass_summaries: list[list[PipelineSummary]] = [[], []]
    per_target: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="jobsift-lever-validation-") as temporary:
        database = Path(temporary) / "jobs.sqlite3"
        csv_path = Path(temporary) / "delivery.csv"
        repository = SQLiteRepository(database)
        for pass_index in range(2):
            for row in targets:
                target = source_target(row)
                collector = LeverCollector(delay=delay)
                observer = ObservedCollector(collector)
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
                pass_summaries[pass_index].append(summary)
                if pass_index == 0:
                    per_target.append(
                        {
                            "instance": row["instance"],
                            "site": row["site"],
                            "status": summary.status,
                            "normalized_jobs": summary.received,
                            "exported": summary.exported,
                            "collector_errors": observer.result.errors[:10],
                        }
                    )
        with repository.connect() as connection:
            decisions = dict(
                connection.execute(
                    "SELECT decision, COUNT(*) FROM job_matches GROUP BY decision ORDER BY decision"
                ).fetchall()
            )
            exports = connection.execute("SELECT COUNT(*) FROM exports").fetchone()[0]
            groups = connection.execute("SELECT COUNT(*) FROM group_deliveries").fetchone()[0]

    def totals(items: list[PipelineSummary]) -> dict[str, int]:
        return {
            field: sum(int(getattr(item, field)) for item in items)
            for field in (
                "received",
                "new",
                "changed",
                "unchanged",
                "matched",
                "rejected",
                "exported",
            )
        }

    return (
        {
            "first_pass": totals(pass_summaries[0]),
            "second_pass": totals(pass_summaries[1]),
            "match_decisions": decisions,
            "stored_export_rows": exports,
            "delivery_groups": groups,
            "second_pass_delivery_suppressed": sum(item.exported for item in pass_summaries[1])
            == 0,
        },
        per_target,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if len(args.commit) != 40:
        parser.error("--commit requires a 40-character SHA")
    if args.delay < 0:
        parser.error("--delay must be non-negative")

    targets = validation_inputs()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or HERE / f"live_{timestamp}"
    output.mkdir(parents=True, exist_ok=False)
    freeze = {
        "started_at_utc": datetime.now(UTC).isoformat(),
        "implementation_commit": args.commit,
        "contract_cohort_manifest_sha256": EXPECTED_MANIFEST,
        "matcher_version": MATCHER_VERSION,
        "dedupe_version": DEDUPE_VERSION,
        "brief_sha256": sha(BRIEF),
        "delay_seconds": args.delay,
        "targets": targets,
        "live_calls": True,
    }
    # Freeze is written before the first provider request.
    write_json(output / "freeze.json", freeze)

    first_results = []
    first_jobs: list[Job] = []
    for row in targets:
        result, _collector, metrics = collect_once(row, delay=args.delay)
        first_results.append({"instance": row["instance"], "site": row["site"], **metrics})
        first_jobs.extend(result.jobs)
    write_json(output / "first_collection.json", first_results)

    second_results = []
    second_jobs: list[Job] = []
    for row in targets:
        result, _collector, metrics = collect_once(row, delay=args.delay)
        second_results.append({"instance": row["instance"], "site": row["site"], **metrics})
        second_jobs.extend(result.jobs)
    write_json(output / "second_collection.json", second_results)

    pipeline, pipeline_targets = pipeline_metrics(targets, delay=args.delay)
    write_json(output / "pipeline_targets.json", pipeline_targets)
    report = {
        "completed_at_utc": datetime.now(UTC).isoformat(),
        "frozen_targets_accounted_for": len(first_results) == len(EXPECTED_TARGETS),
        "first_collection_totals": {
            "normalized": len(first_jobs),
            "statuses": dict(Counter(item["status"] for item in first_results)),
            "quarantined": sum(int(item["quarantined_postings"]) for item in first_results),
        },
        "identity": verify_identity(first_jobs),
        "repeat_stability": repeat_stability(first_jobs, second_jobs),
        "pipeline": pipeline,
    }
    write_json(output / "report.json", report)
    print(json.dumps({"output": str(output), **report}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
