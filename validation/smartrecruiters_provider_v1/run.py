from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from job_scout.collectors.smartrecruiters import SmartRecruitersCollector
from job_scout.domain.daily_batch import DailyBatchRequest
from job_scout.domain.models import CollectionResult, CollectionStatus, MatchDecision, SourceTarget
from job_scout.matching.matcher import match_job
from job_scout.normalization.core import normalize_title
from job_scout.orchestration.daily_batch import finalize_daily_batch, prepare_daily_batch
from job_scout.posting_freshness import posting_freshness_disposition
from job_scout.search_brief import load_search_brief
from job_scout.storage.daily_batches import DailyBatchStore
from job_scout.storage.sqlite import SQLiteRepository


class TargetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identifier: str
    company: str
    employer_id: str | None = None


class BenchmarkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targets: list[TargetConfig] = Field(min_length=1, max_length=20)
    prefilter_titles: bool = False
    max_pages: int = Field(default=2, ge=1, le=10)
    max_postings: int = Field(default=100, ge=1, le=1000)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def canonical_posting_url(value: str, board: str) -> bool:
    url = urlsplit(value)
    parts = url.path.strip("/").split("/")
    return (
        url.scheme == "https"
        and url.hostname == "jobs.smartrecruiters.com"
        and len(parts) == 2
        and parts[0].casefold() == board.casefold()
        and bool(parts[1])
        and not url.username
        and not url.password
    )


def valid_apply_url(value: str, board: str, posting_id: str) -> bool:
    """Require the apply destination to identify the same public SmartRecruiters posting."""
    url = urlsplit(value)
    parts = url.path.strip("/").split("/")
    return (
        url.scheme == "https"
        and url.hostname == "jobs.smartrecruiters.com"
        and url.port in {None, 443}
        and not url.username
        and not url.password
        and len(parts) in {2, 3}
        and parts[0].casefold() == board.casefold()
        and (parts[1] == posting_id or parts[1].startswith(posting_id + "-"))
        and (len(parts) == 2 or parts[2].casefold() == "apply")
    )


def measure_deliverable_supply(jobs, brief, *, completeness="complete") -> dict[str, Any]:
    """Use unchanged production dedupe, freshness, batch policy and history in a sandbox."""
    if os.getenv("TURSO_DATABASE_URL") or os.getenv("TURSO_AUTH_TOKEN"):
        raise ValueError("provider benchmark requires a local-only environment")
    with TemporaryDirectory(prefix="jobsift-sr-benchmark-") as directory:
        repository = SQLiteRepository(Path(directory) / "inventory.db")
        repository.upsert_jobs(jobs)
        now = datetime.now(UTC)
        repository.save_matches(
            match_job(job, brief).model_copy(update={"evaluated_at": now})
            for job in {job.id: job for job in jobs}.values()
        )
        ids = tuple(sorted({job.id for job in jobs}))
        store = DailyBatchStore(repository)

        def request(key):
            return DailyBatchRequest(
                client_id=brief.client_id,
                destination=str(Path(directory) / "dry-run.csv"),
                idempotency_key=key,
                requested_quota=2000,
                max_jobs_per_employer_per_batch=brief.delivery_policy.max_jobs_per_employer_per_batch,
                employer_cooldown_days=brief.delivery_policy.employer_cooldown_days,
                max_posting_age_hours=brief.posting_freshness.max_age_hours,
                unknown_posting_age_policy=brief.posting_freshness.unknown_policy,
                freshness_evaluated_at=now,
                evidence_scope_id="smartrecruiters-benchmark",
                evaluation_id=key,
                candidate_job_ids=ids,
                evidence_sha256=store.evidence_digest(brief.client_id, ids),
                completeness=completeness,
            )

        first = prepare_daily_batch(repository=repository, request=request("first"))
        delivered = finalize_daily_batch(repository=repository, batch_id=first.batch_id)
        if delivered.status != "delivered":
            raise RuntimeError("isolated batch export failed")
        replay = prepare_daily_batch(repository=repository, request=request("replay"))
        return {
            "dry_run_deliverable_jobs": first.selected_count,
            "distinct_matched_employers": first.counts.fresh_eligible_employers,
            "duplicate_postings_collapsed": first.counts.duplicate_postings_collapsed,
            "history_replay_suppressed_groups": replay.counts.previously_delivered_groups,
            "replay_remaining_jobs": replay.selected_count,
            "company_cap_suppressed_groups": first.counts.company_cap_suppressed_groups,
            "history_scope": "isolated benchmark ledger; production client history not loaded",
        }


def validate_benchmark_report(report, *, require_nonempty=False):
    """Fail closed on incomplete collection or incomplete URL contract evidence."""
    totals = report["totals"]
    if totals["targets_failed"] or totals["targets_partial"]:
        raise ValueError("benchmark has failed or partial targets")
    if require_nonempty and (
        totals["normalized_jobs"] <= 0 or totals["postings_with_trustworthy_age"] <= 0
    ):
        raise ValueError("control benchmark lacks jobs or timestamp evidence")
    for key in ("canonical_verified_postings", "valid_apply_url_postings"):
        if totals[key] != totals["normalized_jobs"]:
            raise ValueError(f"incomplete URL coverage: {key}")


def run_benchmark(
    *,
    config: BenchmarkConfig,
    brief_path: Path,
) -> dict[str, Any]:
    brief = load_search_brief(brief_path)
    started_at = datetime.now(UTC)
    target_reports: list[dict[str, Any]] = []
    collected_jobs = []

    for target_config in config.targets:
        target = SourceTarget(
            board_id=target_config.identifier,
            company=target_config.company,
            employer_id=target_config.employer_id,
        )
        timer = time.perf_counter()
        collector = SmartRecruitersCollector(
            title_filter=(
                lambda title: any(
                    normalize_title(role) in normalize_title(title) for role in brief.target_roles
                )
            )
            if config.prefilter_titles
            else None,
            max_pages=config.max_pages,
            max_postings=config.max_postings,
        )
        try:
            result = collector.collect(target)
        except Exception as exc:  # noqa: BLE001 - isolate benchmark targets
            result = CollectionResult(
                source="smartrecruiters",
                target=target,
                status=CollectionStatus.PROVIDER_ERROR,
                errors=[f"{type(exc).__name__}: {exc}"],
                raw_postings_received=0,
            )
        finally:
            collector.client.close()
        collected_jobs.extend(result.jobs)
        evaluated_at = datetime.now(UTC)
        runtime_ms = max(0, round((time.perf_counter() - timer) * 1000))

        timestamped = 0
        fresh_24h = 0
        semantic_matches = 0
        fresh_matches = 0
        canonical_matches = 0
        canonical_urls = 0
        apply_urls = 0
        for job in result.jobs:
            canonical_ok = canonical_posting_url(str(job.canonical_url), target.board_id)
            canonical_urls += canonical_ok
            apply_urls += (
                valid_apply_url(str(job.apply_url), target.board_id, job.source_job_id)
                if job.apply_url
                else 0
            )
            if job.posted_at is not None:
                timestamped += 1
                age_seconds = (evaluated_at - _aware(job.posted_at)).total_seconds()
                if -300 <= age_seconds <= 24 * 3600:
                    fresh_24h += 1

            match = match_job(job, brief).model_copy(update={"evaluated_at": evaluated_at})
            if match.decision not in {
                MatchDecision.STRONG_MATCH,
                MatchDecision.POSSIBLE_MATCH,
            }:
                continue
            semantic_matches += 1
            freshness = posting_freshness_disposition(
                posted_at=job.posted_at,
                max_age_hours=brief.posting_freshness.max_age_hours,
                unknown_policy=brief.posting_freshness.unknown_policy,
                evaluated_at=evaluated_at,
            )
            if freshness is not None:
                continue
            fresh_matches += 1
            if canonical_ok:
                canonical_matches += 1

        target_reports.append(
            {
                "identifier": target_config.identifier,
                "company": target_config.company,
                "status": result.status.value,
                "runtime_ms": runtime_ms,
                "raw_postings": result.raw_postings_received or 0,
                "provider_reported_postings": collector.last_counts.get(
                    "provider_reported_total", 0
                ),
                "indexed_postings": collector.last_counts.get("indexed", 0),
                "normalized_jobs": len(result.jobs),
                "postings_with_trustworthy_age": timestamped,
                "fresh_24h_postings": fresh_24h,
                "semantic_matches": semantic_matches,
                "fresh_searchbrief_matches": fresh_matches,
                "canonical_verified_matches": canonical_matches,
                "canonical_verified_postings": canonical_urls,
                "valid_apply_url_postings": apply_urls,
                "list_requests": collector.last_counts.get("list_requests", 0),
                "detail_requests": collector.last_counts.get("detail_requests", 0),
                "hydrated_postings": collector.last_counts.get("hydrated", 0),
                "quarantined_postings": collector.last_counts.get("quarantined", 0),
                "vanished_postings": collector.last_counts.get("vanished", 0),
                "index_postings_with_trustworthy_age": collector.last_counts.get(
                    "index_timestamped", 0
                ),
                "index_fresh_24h_postings": collector.last_counts.get("index_fresh_24h", 0),
                "plausible_index_matches": collector.last_counts.get("plausible_index_matches", 0),
                "prefilter_suppressed": collector.last_counts.get("prefilter_suppressed", 0),
                "errors": result.errors,
            }
        )

    completed_at = datetime.now(UTC)
    totals = {
        "targets_attempted": len(target_reports),
        "targets_successful": sum(
            report["status"] == CollectionStatus.SUCCESS.value for report in target_reports
        ),
        "targets_partial": sum(
            report["status"] == CollectionStatus.PARTIAL.value for report in target_reports
        ),
        "targets_failed": sum(
            report["status"]
            not in {
                CollectionStatus.SUCCESS.value,
                CollectionStatus.PARTIAL.value,
            }
            for report in target_reports
        ),
        "raw_postings": sum(report["raw_postings"] for report in target_reports),
        "normalized_jobs": sum(report["normalized_jobs"] for report in target_reports),
        "postings_with_trustworthy_age": sum(
            report["postings_with_trustworthy_age"] for report in target_reports
        ),
        "fresh_24h_postings": sum(report["fresh_24h_postings"] for report in target_reports),
        "semantic_matches": sum(report["semantic_matches"] for report in target_reports),
        "fresh_searchbrief_matches": sum(
            report["fresh_searchbrief_matches"] for report in target_reports
        ),
        "canonical_verified_matches": sum(
            report["canonical_verified_matches"] for report in target_reports
        ),
        "canonical_verified_postings": sum(
            report["canonical_verified_postings"] for report in target_reports
        ),
        "valid_apply_url_postings": sum(
            report["valid_apply_url_postings"] for report in target_reports
        ),
        "list_requests": sum(report["list_requests"] for report in target_reports),
        "detail_requests": sum(report["detail_requests"] for report in target_reports),
        "hydrated_postings": sum(report["hydrated_postings"] for report in target_reports),
        "quarantined_postings": sum(report["quarantined_postings"] for report in target_reports),
        "vanished_postings": sum(report["vanished_postings"] for report in target_reports),
        "runtime_ms": sum(report["runtime_ms"] for report in target_reports),
    }
    for key in (
        "provider_reported_postings",
        "indexed_postings",
        "index_postings_with_trustworthy_age",
        "index_fresh_24h_postings",
        "plausible_index_matches",
        "prefilter_suppressed",
    ):
        totals[key] = sum(report[key] for report in target_reports)
    supply = measure_deliverable_supply(
        collected_jobs,
        brief,
        completeness=(
            "complete"
            if totals["targets_partial"] == 0 and totals["targets_failed"] == 0
            else "partial"
        ),
    )
    payload = {
        "supply": supply,
        "benchmark_version": "smartrecruiters-provider-v1",
        "brief": str(brief_path),
        "client_id": brief.client_id,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "config": config.model_dump(mode="json"),
        "totals": totals,
        "targets": target_reports,
    }
    return {**payload, "artifact_sha256": sha256(payload)}


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m validation.smartrecruiters_provider_v1.run")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--brief", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    try:
        config = BenchmarkConfig.model_validate_json(args.config.read_text(encoding="utf-8"))
        report = run_benchmark(config=config, brief_path=args.brief)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(report, sort_keys=True))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        parser.error(f"benchmark failed: {exc}")


if __name__ == "__main__":
    main()
