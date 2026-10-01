"""Bounded production-approved shard benchmark orchestration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from functools import partial
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from job_scout.collectors.workday import WorkdayCollector
from job_scout.domain.models import Job, MatchDecision, SearchBrief
from job_scout.posting_freshness import posting_freshness_disposition
from job_scout.production_registry import (
    CollectionShardManifest,
    ProductionSourceRegistry,
    build_shard_manifest,
    load_production_registry,
    sha256_json,
)
from job_scout.search_brief import load_search_brief
from job_scout.shard_collection import (
    ShardCollectionArtifact,
    collect_shard,
    default_collector_factory,
)
from job_scout.shard_fanin import FanInReport, persist_shard_artifacts
from job_scout.sourcing_plan import evaluate_inventory_run
from job_scout.storage.inventory_runs import InventoryRunStore
from job_scout.storage.sqlite import SQLiteRepository

PROVIDERS = ("greenhouse", "ashby", "workday", "lever")
MAX_BOUNDED_TARGETS = 100


class BenchmarkPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    benchmark_version: Literal["bounded-shard-benchmark-v1"] = "bounded-shard-benchmark-v1"
    parent_registry_id: str
    parent_registry_sha256: str
    benchmark_registry_id: str
    benchmark_registry_sha256: str
    shard_manifest_sha256: str
    target_limits_by_source: dict[str, int]
    shard_counts_by_source: dict[str, int]
    target_counts_by_source: dict[str, int]
    total_targets: int = Field(ge=1, le=MAX_BOUNDED_TARGETS)
    total_shards: int = Field(ge=1)
    workday_detail_concurrency: int = Field(ge=1, le=8)
    matrix: dict[str, list[dict[str, str]]]


class BenchmarkEvaluationMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    client_id: str
    evaluated_at: datetime
    total_evaluated: int = Field(ge=0)
    semantic_matches_before_freshness: int = Field(ge=0)
    freshness_eligible_matches: int = Field(ge=0)
    matcher_non_matches: int = Field(ge=0)
    stale_posting_suppressions: int = Field(ge=0)
    unknown_age_suppressions: int = Field(ge=0)
    invalid_time_suppressions: int = Field(ge=0)


class BenchmarkReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    benchmark_version: Literal["bounded-shard-benchmark-v1"] = "bounded-shard-benchmark-v1"
    fan_in: FanInReport
    evaluation: BenchmarkEvaluationMetrics | None
    active_inventory_jobs: int = Field(ge=0)
    distinct_matched_employers: int = Field(ge=0)
    distinct_matched_delivery_groups: int = Field(ge=0)


def _exact_provider_map(values: dict[str, int], *, label: str) -> dict[str, int]:
    if set(values) != set(PROVIDERS):
        missing = sorted(set(PROVIDERS) - set(values))
        extra = sorted(set(values) - set(PROVIDERS))
        raise ValueError(f"{label} must specify every provider; missing={missing}, extra={extra}")
    if any(value < 1 for value in values.values()):
        raise ValueError(f"{label} values must be positive")
    return {source: values[source] for source in PROVIDERS}


def _stable_target_order(registry: ProductionSourceRegistry, source: str):
    return sorted(
        (target for target in registry.targets if target.source == source),
        key=lambda target: (
            hashlib.sha256(target.target_identity.encode()).hexdigest(),
            target.target_identity,
        ),
    )


def select_registry_subset(
    registry: ProductionSourceRegistry,
    *,
    target_limits_by_source: dict[str, int],
) -> ProductionSourceRegistry:
    """Select a deterministic, production-approved subset for a bounded benchmark."""
    limits = _exact_provider_map(target_limits_by_source, label="target limits")
    selected = []
    for source in PROVIDERS:
        candidates = _stable_target_order(registry, source)
        limit = limits[source]
        if limit > len(candidates):
            raise ValueError(f"target limit exceeds {source} registry size")
        selected.extend(candidates[:limit])
    if len(selected) > MAX_BOUNDED_TARGETS:
        raise ValueError(
            f"bounded benchmark is limited to {MAX_BOUNDED_TARGETS} total targets"
        )
    selected = sorted(selected, key=lambda target: target.target_identity)
    parent_sha = sha256_json(registry.model_dump(mode="json"))
    identity = sha256_json(
        {
            "parent_registry_sha256": parent_sha,
            "target_limits_by_source": limits,
            "target_identities": [target.target_identity for target in selected],
        }
    )[:16]
    return ProductionSourceRegistry(
        registry_id=f"{registry.registry_id}-bounded-{identity}",
        target_universe_git_blob_sha=registry.target_universe_git_blob_sha,
        health_manifest_sha256=registry.health_manifest_sha256,
        health_evidence_updated_at=registry.health_evidence_updated_at,
        approval_policy=registry.approval_policy,
        target_counts_by_source=dict(
            sorted(Counter(target.source for target in selected).items())
        ),
        targets=selected,
    )


def build_benchmark_plan(
    registry: ProductionSourceRegistry,
    *,
    target_limits_by_source: dict[str, int],
    shard_counts_by_source: dict[str, int],
    workday_detail_concurrency: int = 1,
) -> tuple[BenchmarkPlan, ProductionSourceRegistry, CollectionShardManifest]:
    limits = _exact_provider_map(target_limits_by_source, label="target limits")
    shards = _exact_provider_map(shard_counts_by_source, label="shard counts")
    if not 1 <= workday_detail_concurrency <= 8:
        raise ValueError("Workday detail concurrency must be from 1 to 8")
    subset = select_registry_subset(registry, target_limits_by_source=limits)
    for source in PROVIDERS:
        if shards[source] > subset.target_counts_by_source[source]:
            raise ValueError(f"shard count exceeds selected {source} targets")
    manifest = build_shard_manifest(subset, shard_counts_by_source=shards)
    matrix = {
        "include": [
            {"shard_id": shard.shard_id, "source": shard.source}
            for shard in manifest.shards
        ]
    }
    plan = BenchmarkPlan(
        parent_registry_id=registry.registry_id,
        parent_registry_sha256=sha256_json(registry.model_dump(mode="json")),
        benchmark_registry_id=subset.registry_id,
        benchmark_registry_sha256=sha256_json(subset.model_dump(mode="json")),
        shard_manifest_sha256=manifest.manifest_sha256,
        target_limits_by_source=limits,
        shard_counts_by_source=shards,
        target_counts_by_source=subset.target_counts_by_source,
        total_targets=len(subset.targets),
        total_shards=len(manifest.shards),
        workday_detail_concurrency=workday_detail_concurrency,
        matrix=matrix,
    )
    return plan, subset, manifest


def _benchmark_collector_factory(source: str, *, workday_detail_concurrency: int):
    if source == "workday":
        return WorkdayCollector(detail_concurrency=workday_detail_concurrency)
    return default_collector_factory(source)


def _load_benchmark_plan(path: Path) -> BenchmarkPlan:
    return BenchmarkPlan.model_validate_json(path.read_text(encoding="utf-8"))


def _matched_stats(
    repository: SQLiteRepository,
    *,
    run_id: str,
    brief: SearchBrief,
    evaluated_at,
) -> tuple[int, int, int, Counter[str]]:
    active_ids = InventoryRunStore(repository).active_job_ids(run_id)
    if not active_ids:
        return 0, 0, 0, Counter()
    placeholders = ",".join("?" for _ in active_ids)
    with repository.connect() as connection:
        rows = connection.execute(
            "SELECT j.payload_json,g.group_id FROM inventory_run_jobs r "
            "JOIN jobs j ON j.id=r.job_id "
            "JOIN job_matches m ON m.job_id=j.id "
            "JOIN posting_delivery_groups g ON g.job_id=j.id "
            f"WHERE r.run_id=? AND j.id IN ({placeholders}) "
            "AND m.client_id=? AND m.decision IN (?,?)",
            (
                run_id,
                *active_ids,
                brief.client_id,
                MatchDecision.STRONG_MATCH.value,
                MatchDecision.POSSIBLE_MATCH.value,
            ),
        ).fetchall()
    employers = set()
    groups = set()
    freshness_suppressions: Counter[str] = Counter()
    fresh_matches = 0
    for row in rows:
        job = Job.model_validate_json(row["payload_json"])
        disposition = posting_freshness_disposition(
            posted_at=job.posted_at,
            max_age_hours=brief.posting_freshness.max_age_hours,
            unknown_policy=brief.posting_freshness.unknown_policy,
            evaluated_at=evaluated_at,
        )
        if disposition is not None:
            freshness_suppressions[disposition] += 1
            continue
        fresh_matches += 1
        employers.add(job.employer_id or job.company.strip().casefold())
        groups.add(row["group_id"])
    return fresh_matches, len(employers), len(groups), freshness_suppressions

def run_benchmark_fan_in(
    *,
    repository: SQLiteRepository,
    registry: ProductionSourceRegistry,
    manifest: CollectionShardManifest,
    artifacts: list[ShardCollectionArtifact],
    brief_path: Path | None = None,
) -> BenchmarkReport:
    fan_in = persist_shard_artifacts(
        repository=repository,
        registry=registry,
        manifest=manifest,
        artifacts=artifacts,
    )
    evaluation = None
    employer_count = 0
    group_count = 0
    if brief_path is not None and fan_in.status != "failure":
        brief = load_search_brief(brief_path)
        matcher_evaluation = evaluate_inventory_run(
            repository=repository,
            run_id=fan_in.run_id,
            brief=brief,
            evaluated_at=fan_in.collection_completed_at,
        )
        fresh_matches, employer_count, group_count, suppressions = _matched_stats(
            repository,
            run_id=fan_in.run_id,
            brief=brief,
            evaluated_at=matcher_evaluation.evaluated_at,
        )
        evaluation = BenchmarkEvaluationMetrics(
            run_id=matcher_evaluation.run_id,
            client_id=matcher_evaluation.client_id,
            evaluated_at=matcher_evaluation.evaluated_at,
            total_evaluated=matcher_evaluation.total_evaluated,
            semantic_matches_before_freshness=matcher_evaluation.total_matched,
            freshness_eligible_matches=fresh_matches,
            matcher_non_matches=matcher_evaluation.total_rejected,
            stale_posting_suppressions=suppressions["stale_posting"],
            unknown_age_suppressions=suppressions["posting_age_unknown"],
            invalid_time_suppressions=suppressions["posting_time_invalid"],
        )
    return BenchmarkReport(
        fan_in=fan_in,
        evaluation=evaluation,
        active_inventory_jobs=len(InventoryRunStore(repository).active_job_ids(fan_in.run_id)),
        distinct_matched_employers=employer_count,
        distinct_matched_delivery_groups=group_count,
    )


def _assignments(values: list[str], *, label: str) -> dict[str, int]:
    parsed: dict[str, int] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"{label} must use PROVIDER=COUNT")
        source, raw_count = (part.strip().casefold() for part in value.split("=", 1))
        if source in parsed:
            raise ValueError(f"duplicate {label} provider: {source}")
        try:
            parsed[source] = int(raw_count)
        except ValueError as exc:
            raise ValueError(f"{label} count must be an integer: {source}") from exc
    return _exact_provider_map(parsed, label=label)


def require_local_benchmark_environment() -> None:
    if os.getenv("TURSO_DATABASE_URL", "").strip() or os.getenv("TURSO_AUTH_TOKEN", "").strip():
        raise ValueError("bounded benchmark fan-in must not use Turso credentials")


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m job_scout.shard_benchmark")
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan")
    plan.add_argument("--registry", required=True, type=Path)
    plan.add_argument("--output-dir", required=True, type=Path)
    plan.add_argument("--limit", action="append", required=True, default=[])
    plan.add_argument("--shards", action="append", required=True, default=[])
    plan.add_argument("--workday-detail-concurrency", type=int, default=1)

    collect = commands.add_parser("collect")
    collect.add_argument("--registry", required=True, type=Path)
    collect.add_argument("--manifest", required=True, type=Path)
    collect.add_argument("--benchmark-plan", required=True, type=Path)
    collect.add_argument("--shard-id", required=True)
    collect.add_argument("--output", required=True, type=Path)

    fan_in = commands.add_parser("fan-in")
    fan_in.add_argument("--registry", required=True, type=Path)
    fan_in.add_argument("--manifest", required=True, type=Path)
    fan_in.add_argument("--artifacts-dir", required=True, type=Path)
    fan_in.add_argument("--database", required=True, type=Path)
    fan_in.add_argument("--output", required=True, type=Path)
    fan_in.add_argument("--brief", type=Path)

    args = parser.parse_args()
    try:
        if args.command == "plan":
            registry = load_production_registry(args.registry)
            benchmark, subset, manifest = build_benchmark_plan(
                registry,
                target_limits_by_source=_assignments(args.limit, label="target limits"),
                shard_counts_by_source=_assignments(args.shards, label="shard counts"),
                workday_detail_concurrency=args.workday_detail_concurrency,
            )
            _write_json(args.output_dir / "registry.json", subset)
            _write_json(args.output_dir / "manifest.json", manifest)
            _write_json(args.output_dir / "matrix.json", benchmark.matrix)
            _write_json(args.output_dir / "plan.json", benchmark)
            print(json.dumps(benchmark.model_dump(mode="json"), sort_keys=True))
            return

        registry = load_production_registry(args.registry)
        manifest = CollectionShardManifest.model_validate_json(
            args.manifest.read_text(encoding="utf-8")
        )
        if args.command == "collect":
            benchmark_plan = _load_benchmark_plan(args.benchmark_plan)
            if (
                benchmark_plan.benchmark_registry_id != registry.registry_id
                or benchmark_plan.shard_manifest_sha256 != manifest.manifest_sha256
            ):
                raise ValueError("benchmark plan does not match registry/manifest")
            artifact = collect_shard(
                registry=registry,
                manifest=manifest,
                shard_id=args.shard_id,
                collector_factory=partial(
                    _benchmark_collector_factory,
                    workday_detail_concurrency=benchmark_plan.workday_detail_concurrency,
                ),
            )
            _write_json(args.output, artifact)
            print(json.dumps(artifact.model_dump(mode="json"), sort_keys=True))
            return

        artifact_paths = sorted(args.artifacts_dir.glob("*.json"))
        if not artifact_paths:
            raise ValueError("no shard artifact files found")
        artifacts = [
            ShardCollectionArtifact.model_validate_json(path.read_text(encoding="utf-8"))
            for path in artifact_paths
        ]
        require_local_benchmark_environment()
        args.database.parent.mkdir(parents=True, exist_ok=True)
        report = run_benchmark_fan_in(
            repository=SQLiteRepository(args.database),
            registry=registry,
            manifest=manifest,
            artifacts=artifacts,
            brief_path=args.brief,
        )
        _write_json(args.output, report)
        print(json.dumps(report.model_dump(mode="json"), sort_keys=True))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        parser.error(f"benchmark failed: {exc}")


if __name__ == "__main__":
    main()
