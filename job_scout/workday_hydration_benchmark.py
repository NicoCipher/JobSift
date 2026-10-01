"""JOB-37: hydrate the frozen trusted Workday index candidates and measure yield."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from job_scout.collectors.workday import WorkdayCollector
from job_scout.dedupe.resolver import representative_key
from job_scout.domain.models import Job, MatchDecision
from job_scout.matching.matcher import match_job
from job_scout.normalization.company import employer_key
from job_scout.posting_freshness import posting_freshness_disposition
from job_scout.production_registry import load_production_registry, sha256_json
from job_scout.search_brief import load_search_brief
from job_scout.storage.sqlite import SQLiteRepository


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return value


def _verify_self_digest(value: dict[str, Any], field: str) -> None:
    expected = value.get(field)
    if not isinstance(expected, str) or len(expected) != 64:
        raise ValueError(f"{field} is missing or invalid")
    payload = {key: item for key, item in value.items() if key != field}
    if sha256_json(payload) != expected:
        raise ValueError(f"{field} does not match payload")


def _identity_digest(job: Job) -> bytes:
    payload = json.dumps(
        [job.source, job.source_board_id, job.source_job_id],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(payload).digest()


def _url_digest(job: Job) -> bytes:
    return hashlib.sha256(str(job.canonical_url).encode()).digest()


def _load_historical_evidence(path: Path, *, client_id: str) -> dict[str, Any]:
    metadata = _load_json(path)
    _verify_self_digest(metadata, "evidence_sha256")
    if metadata.get("evidence_version") != "workday-historical-candidate-suppression-v1":
        raise ValueError("unsupported historical suppression evidence")
    if metadata.get("client_id") != client_id:
        raise ValueError("historical suppression client does not match SearchBrief")

    raw_identity_hashes = metadata.get("matching_identity_hashes")
    raw_url_hashes = metadata.get("matching_url_hashes")
    if not isinstance(raw_identity_hashes, list) or not isinstance(raw_url_hashes, list):
        raise TypeError("historical suppression hashes are invalid")
    try:
        identity_hashes = {bytes.fromhex(value) for value in raw_identity_hashes}
        url_hashes = {bytes.fromhex(value) for value in raw_url_hashes}
    except (TypeError, ValueError) as exc:
        raise ValueError("historical suppression hashes are not valid SHA-256 hex") from exc
    if any(len(value) != 32 for value in identity_hashes | url_hashes):
        raise ValueError("historical suppression hashes are not SHA-256 digests")
    if len(identity_hashes) != len(raw_identity_hashes):
        raise ValueError("historical identity hashes contain duplicates")
    if len(url_hashes) != len(raw_url_hashes):
        raise ValueError("historical URL hashes contain duplicates")

    return {
        "metadata": metadata,
        "identity_hashes": identity_hashes,
        "url_hashes": url_hashes,
    }

def _is_historically_surfaced(job: Job, ledger: dict[str, Any]) -> bool:
    return (
        _identity_digest(job) in ledger["identity_hashes"]
        or _url_digest(job) in ledger["url_hashes"]
    )


def _target_identity(job: Job) -> str:
    return f"workday:{job.source_board_id}"


def _freshness_reason(job: Job, brief, evaluated_at: datetime) -> str | None:
    return posting_freshness_disposition(
        posted_at=job.posted_at,
        max_age_hours=brief.posting_freshness.max_age_hours,
        unknown_policy=brief.posting_freshness.unknown_policy,
        evaluated_at=evaluated_at,
    )


def run_hydration_benchmark(
    *,
    registry_path: Path,
    candidates_path: Path,
    historical_hashes_path: Path,
    brief_path: Path,
    database_path: Path,
    detail_concurrency: int = 4,
    expected_candidates: int = 140,
) -> dict[str, Any]:
    if candidates_path.suffix == ".gz":
        candidates_manifest = json.loads(gzip.decompress(candidates_path.read_bytes()))
        if not isinstance(candidates_manifest, dict):
            raise ValueError(f"{candidates_path} must contain a JSON object")
    else:
        candidates_manifest = _load_json(candidates_path)
    _verify_self_digest(candidates_manifest, "manifest_sha256")
    if candidates_manifest.get("manifest_version") != "workday-hydration-candidates-v2":
        raise ValueError("unsupported candidate manifest")
    candidates = candidates_manifest.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != expected_candidates:
        raise ValueError(
            f"trusted candidate count must be {expected_candidates}, got "
            f"{len(candidates) if isinstance(candidates, list) else 'invalid'}"
        )
    if candidates_manifest.get("trusted_candidate_count") != len(candidates):
        raise ValueError("candidate manifest count does not reconcile")

    candidate_keys: list[tuple[str, str]] = []
    for row in candidates:
        if not isinstance(row, dict):
            raise TypeError("candidate manifest contains invalid candidate rows")
        target_identity = row.get("target_identity")
        external_path = row.get("external_path")
        if not isinstance(target_identity, str) or not isinstance(external_path, str):
            raise TypeError("candidate manifest contains invalid candidate rows")
        candidate_keys.append((target_identity, external_path))
    if len(set(candidate_keys)) != len(candidate_keys):
        raise ValueError("candidate manifest contains duplicate target/path candidates")

    brief = load_search_brief(brief_path)
    if candidates_manifest.get("brief_client_id") != brief.client_id:
        raise ValueError("candidate manifest client does not match SearchBrief")
    historical = _load_historical_evidence(
        historical_hashes_path,
        client_id=brief.client_id,
    )
    if historical["metadata"].get("source_candidate_manifest_sha256") != candidates_manifest.get(
        "manifest_sha256"
    ):
        raise ValueError("historical suppression evidence does not match candidate manifest")

    registry = load_production_registry(registry_path)
    targets = {target.target_identity: target for target in registry.targets}
    grouped_paths: dict[str, list[str]] = defaultdict(list)
    for target_identity, external_path in candidate_keys:
        target = targets.get(target_identity)
        if target is None or target.source != "workday":
            raise ValueError(f"candidate references non-approved Workday target: {target_identity}")
        grouped_paths[target_identity].append(external_path)

    started_at = datetime.now(UTC)
    hydrated_jobs: list[Job] = []
    target_reports: list[dict[str, Any]] = []
    total_hydration_errors = 0
    collector = WorkdayCollector(detail_concurrency=detail_concurrency)
    try:
        for target_identity in sorted(grouped_paths):
            paths = grouped_paths[target_identity]
            result = collector.hydrate_paths(targets[target_identity].source_target(), paths)
            hydrated_jobs.extend(result.jobs)
            error_count = len(result.errors)
            total_hydration_errors += error_count
            target_reports.append(
                {
                    "target_identity": target_identity,
                    "candidate_paths": len(paths),
                    "successful_detail_paths": len(paths) - error_count,
                    "hydrated_unique_jobs": len(result.jobs),
                    "hydration_failures": error_count,
                    "provider_identity_collapses": max(
                        0, len(paths) - error_count - len(result.jobs)
                    ),
                    "status": result.status.value,
                    "errors": list(result.errors),
                }
            )
    finally:
        client = getattr(collector, "client", None)
        if client is not None:
            client.close()

    evaluated_at = datetime.now(UTC)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    repository = SQLiteRepository(database_path)
    repository.upsert_jobs(hydrated_jobs)

    matches = [
        match_job(job, brief).model_copy(update={"evaluated_at": evaluated_at})
        for job in hydrated_jobs
    ]
    repository.save_matches(matches)
    match_by_job = {match.job_id: match for match in matches}
    fresh_by_job = {
        job.id: _freshness_reason(job, brief, evaluated_at) for job in hydrated_jobs
    }
    historical_by_job = {
        job.id: _is_historically_surfaced(job, historical) for job in hydrated_jobs
    }

    group_by_job: dict[str, str] = {}
    with repository.connect() as connection:
        for row in connection.execute(
            "SELECT job_id,group_id FROM posting_delivery_groups ORDER BY job_id"
        ).fetchall():
            group_by_job[row["job_id"]] = row["group_id"]

    semantic_jobs = [
        job
        for job in hydrated_jobs
        if match_by_job[job.id].decision
        in {MatchDecision.STRONG_MATCH, MatchDecision.POSSIBLE_MATCH}
    ]
    semantic_groups: dict[str, list[Job]] = defaultdict(list)
    for job in semantic_jobs:
        semantic_groups[group_by_job[job.id]].append(job)

    historical_groups = {
        group
        for group, members in semantic_groups.items()
        if any(historical_by_job[job.id] for job in members)
    }

    fresh_groups: dict[str, list[Job]] = {}
    freshness_group_suppressions: Counter[str] = Counter()
    for group, members in semantic_groups.items():
        if group in historical_groups:
            continue
        eligible = [job for job in members if fresh_by_job[job.id] is None]
        if eligible:
            fresh_groups[group] = eligible
            continue
        reasons = {fresh_by_job[job.id] for job in members}
        reason = (
            "posting_time_invalid"
            if "posting_time_invalid" in reasons
            else "posting_age_unknown"
            if "posting_age_unknown" in reasons
            else "stale_posting"
        )
        freshness_group_suppressions[reason] += 1

    representatives = {
        group: min(members, key=representative_key)
        for group, members in fresh_groups.items()
    }
    contribution = Counter(_target_identity(job) for job in representatives.values())
    employers = Counter(employer_key(job) for job in representatives.values())
    employer_cap = brief.delivery_policy.max_jobs_per_employer_per_batch
    delivery_policy_eligible = (
        len(representatives)
        if employer_cap is None
        else sum(min(count, employer_cap) for count in employers.values())
    )

    target_index = {row["target_identity"]: row for row in target_reports}
    for target_identity, count in sorted(contribution.items()):
        target_index[target_identity]["fresh_unique_representatives"] = count
    for row in target_reports:
        row.setdefault("fresh_unique_representatives", 0)

    freshness_postings = Counter(
        fresh_by_job[job.id] or "fresh" for job in hydrated_jobs
    )
    match_decisions = Counter(match.decision.value for match in matches)
    unique_hydrated_identity_count = len(
        {(job.source, job.source_board_id, job.source_job_id) for job in hydrated_jobs}
    )
    successful_detail_paths = len(candidates) - total_hydration_errors
    provider_identity_collapses = max(
        0, successful_detail_paths - unique_hydrated_identity_count
    )

    report = {
        "report_version": "workday-hydration-yield-v1",
        "job": "JOB-37",
        "started_at": started_at.isoformat(),
        "evaluated_at": evaluated_at.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "source_index_run_id": candidates_manifest["source_index_run_id"],
        "candidate_manifest_sha256": candidates_manifest["manifest_sha256"],
        "historical_evidence_sha256": historical["metadata"]["evidence_sha256"],
        "historical_logical_corpus_sha256": historical["metadata"][
            "logical_corpus_sha256"
        ],
        "historical_probe_run_id": historical["metadata"]["hydration_probe_run_id"],
        "client_id": brief.client_id,
        "posting_age_source": "Workday jobPostingInfo.startDate parsed by production collector",
        "freshness_policy": {
            "max_age_hours": brief.posting_freshness.max_age_hours,
            "unknown_policy": brief.posting_freshness.unknown_policy.value,
        },
        "candidate_paths_attempted": len(candidates),
        "targets_with_candidates": len(grouped_paths),
        "successful_detail_paths": successful_detail_paths,
        "hydration_failures": total_hydration_errors,
        "hydrated_unique_jobs": len(hydrated_jobs),
        "unique_hydrated_provider_identities": unique_hydrated_identity_count,
        "provider_identity_collapses": provider_identity_collapses,
        "freshness_postings": dict(sorted(freshness_postings.items())),
        "match_decisions": dict(sorted(match_decisions.items())),
        "semantic_match_postings": len(semantic_jobs),
        "semantic_match_groups": len(semantic_groups),
        "historically_surfaced_postings": sum(historical_by_job.values()),
        "historically_suppressed_groups": len(historical_groups),
        "prior_delivery_suppressed_groups": 0,
        "prior_delivery_evidence": (
            "No production JobSift delivery ledger exists yet; this isolated capacity "
            "benchmark therefore has zero prior JobSift delivery suppressions."
        ),
        "freshness_group_suppressions": dict(sorted(freshness_group_suppressions.items())),
        "practical_duplicate_postings_collapsed": len(semantic_jobs) - len(semantic_groups),
        "fresh_unique_delivery_groups": len(representatives),
        "fresh_unique_employers": len(employers),
        "delivery_policy_max_jobs_per_employer": employer_cap,
        "final_delivery_policy_eligible_count": delivery_policy_eligible,
        "per_target": sorted(target_reports, key=lambda row: row["target_identity"]),
    }
    return report


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m job_scout.workday_hydration_benchmark")
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--candidates", required=True, type=Path)
    parser.add_argument("--historical-hashes", required=True, type=Path)
    parser.add_argument("--brief", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--detail-concurrency", type=int, default=4)
    parser.add_argument("--expected-candidates", type=int, default=140)
    args = parser.parse_args()
    try:
        if not 1 <= args.detail_concurrency <= 8:
            raise ValueError("detail concurrency must be from 1 to 8")
        report = run_hydration_benchmark(
            registry_path=args.registry,
            candidates_path=args.candidates,
            historical_hashes_path=args.historical_hashes,
            brief_path=args.brief,
            database_path=args.database,
            detail_concurrency=args.detail_concurrency,
            expected_candidates=args.expected_candidates,
        )
        _write_json(args.output, report)
        print(json.dumps(report, sort_keys=True))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        parser.error(f"JOB-37 hydration benchmark failed: {exc}")


if __name__ == "__main__":
    main()
