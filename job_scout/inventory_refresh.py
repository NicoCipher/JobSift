"""Scheduled rotating refresh of the production-approved shared job inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from collections import Counter
from pathlib import Path

from pydantic import BaseModel

from job_scout.production_registry import (
    CollectionShardManifest,
    ProductionSourceRegistry,
    load_production_registry,
    sha256_json,
)
from job_scout.shard_benchmark import PROVIDERS, build_benchmark_plan
from job_scout.shard_collection import ShardCollectionArtifact
from job_scout.shard_fanin import persist_shard_artifacts
from job_scout.storage.sqlite import SQLiteRepository

DEFAULT_LIMITS = {
    "greenhouse": 40,
    "ashby": 40,
    "workday": 1,
    "lever": 19,
    "smartrecruiters": 1,
}
DEFAULT_SHARDS = {
    "greenhouse": 6,
    "ashby": 6,
    "workday": 1,
    "lever": 4,
    "smartrecruiters": 1,
}
DEFAULT_WORKDAY_DETAIL_CONCURRENCY = 4
MAX_REFRESH_TARGETS = 125
WORKDAY_RAMP_LEVELS = (1, 5, 10, 20, 25)


def default_refresh_limits(
    registry: ProductionSourceRegistry,
    *,
    workday_limit: int | None = None,
) -> dict[str, int]:
    limits = {
        source: DEFAULT_LIMITS[source]
        for source in PROVIDERS
        if source in registry.target_counts_by_source
    }
    if "workday" in limits and workday_limit is not None:
        if workday_limit not in WORKDAY_RAMP_LEVELS:
            raise ValueError(
                f"Workday refresh limit must be one of {WORKDAY_RAMP_LEVELS}"
            )
        limits["workday"] = min(
            workday_limit,
            registry.target_counts_by_source["workday"],
        )
    # SmartRecruiters is additive only after explicit registry admission.
    if "smartrecruiters" in limits and "greenhouse" in limits:
        limits["greenhouse"] -= limits["smartrecruiters"]
    if sum(limits.values()) > MAX_REFRESH_TARGETS:
        raise ValueError("default refresh cohort exceeds safety ceiling")
    return limits


def _stable(values):
    return sorted(
        values,
        key=lambda target: (
            hashlib.sha256(target.target_identity.encode()).hexdigest(),
            target.target_identity,
        ),
    )


def _refresh_limits(
    registry: ProductionSourceRegistry,
    limits: dict[str, int] | None,
    *,
    cohort: int,
) -> dict[str, int]:
    if limits is not None:
        values = dict(limits)
    else:
        values = default_refresh_limits(registry)
    if sum(values.values()) > MAX_REFRESH_TARGETS:
        raise ValueError(f"refresh cohort exceeds {MAX_REFRESH_TARGETS}-target safety ceiling")
    return values


def rotating_registry(
    registry: ProductionSourceRegistry,
    *,
    cohort: int,
    limits: dict[str, int] | None = None,
) -> ProductionSourceRegistry:
    """Return one deterministic rotating cohort without weakening source approval."""
    if cohort < 0:
        raise ValueError("cohort must be non-negative")
    providers = tuple(source for source in PROVIDERS if source in registry.target_counts_by_source)
    limits = _refresh_limits(registry, limits, cohort=cohort)
    if set(limits) != set(providers) or any(value < 1 for value in limits.values()):
        raise ValueError("refresh limits must contain positive counts for every provider")

    selected = []
    for source in providers:
        candidates = _stable(target for target in registry.targets if target.source == source)
        limit = limits[source]
        if limit > len(candidates):
            raise ValueError(f"refresh limit exceeds {source} registry size")
        start = (cohort * limit) % len(candidates)
        chosen = [candidates[(start + offset) % len(candidates)] for offset in range(limit)]
        selected.extend(chosen)

    selected.sort(key=lambda target: target.target_identity)
    parent_sha = sha256_json(registry.model_dump(mode="json"))
    identity = sha256_json(
        {
            "parent_registry_sha256": parent_sha,
            "cohort": cohort,
            "limits": limits,
            "targets": [target.target_identity for target in selected],
        }
    )[:16]
    return ProductionSourceRegistry(
        registry_id=f"{registry.registry_id}-refresh-{cohort}-{identity}",
        target_universe_git_blob_sha=registry.target_universe_git_blob_sha,
        health_manifest_sha256=registry.health_manifest_sha256,
        health_evidence_updated_at=registry.health_evidence_updated_at,
        approval_policy=registry.approval_policy,
        target_counts_by_source=dict(
            sorted(Counter(target.source for target in selected).items())
        ),
        targets=selected,
    )


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_refresh_plan(
    *,
    registry: ProductionSourceRegistry,
    cohort: int,
    limits: dict[str, int] | None = None,
    shards: dict[str, int] | None = None,
    workday_detail_concurrency: int = DEFAULT_WORKDAY_DETAIL_CONCURRENCY,
):
    providers = tuple(source for source in PROVIDERS if source in registry.target_counts_by_source)
    limits = _refresh_limits(registry, limits, cohort=cohort)
    if shards is None:
        shards = {source: DEFAULT_SHARDS[source] for source in providers}
        if "workday" in shards:
            # Keep Workday shards small enough to isolate provider failures while
            # bounding matrix growth. Detail concurrency remains independently capped.
            shards["workday"] = min(limits["workday"], max(1, math.ceil(limits["workday"] / 5)))
    else:
        shards = dict(shards)
    rotating = rotating_registry(registry, cohort=cohort, limits=limits)
    plan, subset, manifest = build_benchmark_plan(
        rotating,
        target_limits_by_source=limits,
        shard_counts_by_source=shards,
        workday_detail_concurrency=workday_detail_concurrency,
    )
    return plan, subset, manifest


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m job_scout.inventory_refresh")
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan")
    plan.add_argument("--registry", type=Path, required=True)
    plan.add_argument("--cohort", type=int, required=True)
    plan.add_argument("--output-dir", type=Path, required=True)
    plan.add_argument(
        "--workday-limit",
        type=int,
        choices=WORKDAY_RAMP_LEVELS,
        default=DEFAULT_LIMITS["workday"],
        help="Explicit guarded Workday ramp level; never auto-escalates.",
    )

    fan_in = commands.add_parser("fan-in")
    fan_in.add_argument("--registry", type=Path, required=True)
    fan_in.add_argument("--manifest", type=Path, required=True)
    fan_in.add_argument("--artifacts-dir", type=Path, required=True)
    fan_in.add_argument("--database", type=Path, required=True)
    fan_in.add_argument("--output", type=Path, required=True)
    fan_in.add_argument("--retention-hours", type=int, default=72)

    args = parser.parse_args()
    try:
        if args.command == "plan":
            registry = load_production_registry(args.registry)
            limits = default_refresh_limits(
                registry,
                workday_limit=args.workday_limit,
            )
            refresh, subset, manifest = build_refresh_plan(
                registry=registry,
                cohort=args.cohort,
                limits=limits,
            )
            _write_json(args.output_dir / "registry.json", subset)
            _write_json(args.output_dir / "manifest.json", manifest)
            _write_json(args.output_dir / "matrix.json", refresh.matrix)
            _write_json(args.output_dir / "plan.json", refresh)
            _write_json(
                args.output_dir / "refresh.json",
                {
                    "cohort": args.cohort,
                    "parent_registry_id": registry.registry_id,
                    "parent_registry_sha256": sha256_json(
                        registry.model_dump(mode="json")
                    ),
                    "selected_registry_id": subset.registry_id,
                    "selected_targets": len(subset.targets),
                    "target_counts_by_source": subset.target_counts_by_source,
                },
            )
            print(json.dumps(refresh.model_dump(mode="json"), sort_keys=True))
            return

        registry = load_production_registry(args.registry)
        manifest = CollectionShardManifest.model_validate_json(
            args.manifest.read_text(encoding="utf-8")
        )
        artifact_paths = sorted(args.artifacts_dir.glob("*.json"))
        if not artifact_paths:
            raise ValueError("no shard artifact files found")
        artifact_load_started = time.perf_counter()
        artifacts = [
            ShardCollectionArtifact.model_validate_json(path.read_text(encoding="utf-8"))
            for path in artifact_paths
        ]
        print(
            json.dumps(
                {
                    "event": "inventory_refresh_artifacts_loaded",
                    "shards": len(artifacts),
                    "elapsed_ms": round(
                        (time.perf_counter() - artifact_load_started) * 1000
                    ),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        args.database.parent.mkdir(parents=True, exist_ok=True)
        repository_started = time.perf_counter()
        repository = SQLiteRepository(args.database)
        print(
            json.dumps(
                {
                    "event": "inventory_refresh_repository_ready",
                    "elapsed_ms": round((time.perf_counter() - repository_started) * 1000),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        fan_in_started = time.perf_counter()
        report = persist_shard_artifacts(
            repository=repository,
            registry=registry,
            manifest=manifest,
            artifacts=artifacts,
            payload_retention_hours=args.retention_hours,
        )
        fan_in_seconds = time.perf_counter() - fan_in_started
        print(
            json.dumps(
                {
                    "event": "inventory_refresh_fan_in_complete",
                    "elapsed_ms": round(fan_in_seconds * 1000),
                    "normalized_jobs": report.metrics.unique_normalized_jobs,
                    "inventory_memberships": report.metrics.inventory_memberships,
                    "replayed": report.replayed,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        retention_started = time.perf_counter()
        retention = repository.prune_stale_inventory(
            retention_hours=args.retention_hours
        )
        retention_seconds = time.perf_counter() - retention_started
        print(
            json.dumps(
                {
                    "event": "inventory_refresh_prune_complete",
                    "elapsed_ms": round(retention_seconds * 1000),
                    **retention,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        payload = {
            "fan_in": report.model_dump(mode="json"),
            "retention": retention,
            "timings": {
                "fan_in_seconds": round(fan_in_seconds, 3),
                "retention_seconds": round(retention_seconds, 3),
            },
        }
        _write_json(args.output, payload)
        print(json.dumps(payload, sort_keys=True))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        parser.error(f"inventory refresh failed: {exc}")


if __name__ == "__main__":
    main()
