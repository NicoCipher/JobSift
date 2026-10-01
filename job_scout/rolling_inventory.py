"""Scheduled rotating collection over the production-approved source registry."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from job_scout.collectors.workday import WorkdayCollector
from job_scout.production_registry import (
    CollectionShardManifest,
    ProductionSourceRegistry,
    build_shard_manifest,
    load_production_registry,
    sha256_json,
)
from job_scout.shard_collection import (
    ShardCollectionArtifact,
    collect_shard,
    default_collector_factory,
)
from job_scout.shard_fanin import persist_shard_artifacts
from job_scout.storage.sqlite import SQLiteRepository

PROVIDERS = ("greenhouse", "ashby", "workday", "lever")
DEFAULT_LIMITS = {"greenhouse": 35, "ashby": 35, "workday": 20, "lever": 10}
DEFAULT_SHARDS = {"greenhouse": 5, "ashby": 5, "workday": 4, "lever": 2}


def _stable_targets(registry: ProductionSourceRegistry, source: str):
    return sorted(
        (target for target in registry.targets if target.source == source),
        key=lambda target: (
            hashlib.sha256(target.target_identity.encode()).hexdigest(),
            target.target_identity,
        ),
    )


def _take_rotating(values: list, *, slot: int, count: int) -> list:
    if count < 1:
        raise ValueError("refresh counts must be positive")
    if count > len(values):
        raise ValueError("refresh count exceeds provider target count")
    start = (slot * count) % len(values)
    return [values[(start + offset) % len(values)] for offset in range(count)]


def build_refresh_registry(
    registry: ProductionSourceRegistry,
    *,
    slot: int,
    target_counts: dict[str, int] | None = None,
) -> ProductionSourceRegistry:
    """Choose one deterministic circular window per provider for a 30-minute slot."""
    if slot < 0:
        raise ValueError("rotation slot must be non-negative")
    counts = dict(target_counts or DEFAULT_LIMITS)
    if set(counts) != set(PROVIDERS):
        raise ValueError("refresh target counts must specify every provider")
    selected = []
    for source in PROVIDERS:
        selected.extend(
            _take_rotating(
                _stable_targets(registry, source),
                slot=slot,
                count=counts[source],
            )
        )
    selected = sorted(selected, key=lambda target: target.target_identity)
    return ProductionSourceRegistry(
        registry_id=f"{registry.registry_id}-rolling-{slot}",
        target_universe_git_blob_sha=registry.target_universe_git_blob_sha,
        health_manifest_sha256=registry.health_manifest_sha256,
        health_evidence_updated_at=registry.health_evidence_updated_at,
        approval_policy=registry.approval_policy,
        target_counts_by_source=dict(
            sorted(Counter(target.source for target in selected).items())
        ),
        targets=selected,
    )


def _slot(value: int | None) -> int:
    if value is not None:
        return value
    return int(datetime.now(UTC).timestamp() // 1800)


def _collector_factory(source: str, *, workday_detail_concurrency: int):
    if source == "workday":
        return WorkdayCollector(detail_concurrency=workday_detail_concurrency)
    return default_collector_factory(source)


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def plan(
    *,
    registry_path: Path,
    output_dir: Path,
    slot: int | None = None,
    workday_detail_concurrency: int = 4,
) -> dict[str, object]:
    parent = load_production_registry(registry_path)
    rotation_slot = _slot(slot)
    selected = build_refresh_registry(parent, slot=rotation_slot)
    manifest = build_shard_manifest(
        selected,
        shard_counts_by_source=DEFAULT_SHARDS,
    )
    matrix = {
        "include": [
            {"shard_id": shard.shard_id, "source": shard.source}
            for shard in manifest.shards
        ]
    }
    payload = {
        "refresh_version": "rolling-inventory-v1",
        "rotation_slot": rotation_slot,
        "parent_registry_id": parent.registry_id,
        "parent_registry_sha256": sha256_json(parent.model_dump(mode="json")),
        "registry_id": selected.registry_id,
        "registry_sha256": sha256_json(selected.model_dump(mode="json")),
        "manifest_sha256": manifest.manifest_sha256,
        "target_counts_by_source": selected.target_counts_by_source,
        "total_targets": len(selected.targets),
        "workday_detail_concurrency": workday_detail_concurrency,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "registry.json", selected.model_dump(mode="json"))
    _write_json(output_dir / "manifest.json", manifest.model_dump(mode="json"))
    _write_json(output_dir / "matrix.json", matrix)
    _write_json(output_dir / "plan.json", payload)
    return payload


def collect(
    *,
    registry_path: Path,
    manifest_path: Path,
    shard_id: str,
    output_path: Path,
    workday_detail_concurrency: int = 4,
) -> ShardCollectionArtifact:
    registry = ProductionSourceRegistry.model_validate_json(
        registry_path.read_text(encoding="utf-8")
    )
    manifest = CollectionShardManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    artifact = collect_shard(
        registry=registry,
        manifest=manifest,
        shard_id=shard_id,
        collector_factory=partial(
            _collector_factory,
            workday_detail_concurrency=workday_detail_concurrency,
        ),
    )
    _write_json(output_path, artifact.model_dump(mode="json"))
    return artifact


def fan_in(
    *,
    database: Path,
    registry_path: Path,
    manifest_path: Path,
    artifacts_dir: Path,
    output_path: Path,
) -> dict[str, object]:
    registry = ProductionSourceRegistry.model_validate_json(
        registry_path.read_text(encoding="utf-8")
    )
    manifest = CollectionShardManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    artifacts = [
        ShardCollectionArtifact.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(artifacts_dir.rglob("*.json"))
    ]
    report = persist_shard_artifacts(
        repository=SQLiteRepository(database),
        registry=registry,
        manifest=manifest,
        artifacts=artifacts,
    )
    payload = report.model_dump(mode="json")
    _write_json(output_path, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m job_scout.rolling_inventory")
    commands = parser.add_subparsers(dest="command", required=True)

    plan_cmd = commands.add_parser("plan")
    plan_cmd.add_argument("--registry", required=True, type=Path)
    plan_cmd.add_argument("--output-dir", required=True, type=Path)
    plan_cmd.add_argument("--slot", type=int)
    plan_cmd.add_argument("--workday-detail-concurrency", type=int, default=4)

    collect_cmd = commands.add_parser("collect")
    collect_cmd.add_argument("--registry", required=True, type=Path)
    collect_cmd.add_argument("--manifest", required=True, type=Path)
    collect_cmd.add_argument("--shard-id", required=True)
    collect_cmd.add_argument("--output", required=True, type=Path)
    collect_cmd.add_argument("--workday-detail-concurrency", type=int, default=4)

    fan_cmd = commands.add_parser("fan-in")
    fan_cmd.add_argument("--database", required=True, type=Path)
    fan_cmd.add_argument("--registry", required=True, type=Path)
    fan_cmd.add_argument("--manifest", required=True, type=Path)
    fan_cmd.add_argument("--artifacts-dir", required=True, type=Path)
    fan_cmd.add_argument("--output", required=True, type=Path)

    args = parser.parse_args()
    if args.command == "plan":
        result = plan(
            registry_path=args.registry,
            output_dir=args.output_dir,
            slot=args.slot,
            workday_detail_concurrency=args.workday_detail_concurrency,
        )
    elif args.command == "collect":
        result = collect(
            registry_path=args.registry,
            manifest_path=args.manifest,
            shard_id=args.shard_id,
            output_path=args.output,
            workday_detail_concurrency=args.workday_detail_concurrency,
        ).model_dump(mode="json")
    else:
        result = fan_in(
            database=args.database,
            registry_path=args.registry,
            manifest_path=args.manifest,
            artifacts_dir=args.artifacts_dir,
            output_path=args.output,
        )
    print(json.dumps(result, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
