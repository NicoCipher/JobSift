"""Versioned production-approved ATS target registry and deterministic sharding."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROVIDERS = ("greenhouse", "ashby", "workday", "lever")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class ProductionTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_identity: str
    source: Literal["greenhouse", "ashby", "workday", "lever"]
    coordinates: dict[str, str]
    company_hint: str | None = None
    health_classification: Literal["active"] = "active"
    health_current_postings: int | None = Field(default=None, ge=0)
    health_inventory_exact: bool | None = None


class ProductionSourceRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registry_version: Literal["production-source-registry-v1"] = "production-source-registry-v1"
    registry_id: str
    target_universe_git_blob_sha: str = Field(min_length=40, max_length=40)
    health_manifest_sha256: str = Field(min_length=64, max_length=64)
    health_evidence_updated_at: str
    approval_policy: Literal["health-active-only"] = "health-active-only"
    target_counts_by_source: dict[str, int]
    targets: list[ProductionTarget]
    registry_sha256: str

    @model_validator(mode="after")
    def verify_registry(self) -> "ProductionSourceRegistry":
        identities = [target.target_identity for target in self.targets]
        if len(identities) != len(set(identities)):
            raise ValueError("production registry contains duplicate target identities")
        expected_counts = dict(sorted(Counter(target.source for target in self.targets).items()))
        if self.target_counts_by_source != expected_counts:
            raise ValueError("production registry source counts do not reconcile")
        payload = self.model_dump(mode="json", exclude={"registry_sha256"})
        if self.registry_sha256 != sha256_json(payload):
            raise ValueError("production registry hash is invalid")
        return self


class CollectionShard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shard_id: str
    source: Literal["greenhouse", "ashby", "workday", "lever"]
    target_identities: list[str]


class CollectionShardManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_version: Literal["collection-shards-v1"] = "collection-shards-v1"
    registry_id: str
    registry_sha256: str
    shard_counts_by_source: dict[str, int]
    target_counts_by_source: dict[str, int]
    shards: list[CollectionShard]
    manifest_sha256: str

    @model_validator(mode="after")
    def verify_manifest(self) -> "CollectionShardManifest":
        ids = [identity for shard in self.shards for identity in shard.target_identities]
        if len(ids) != len(set(ids)):
            raise ValueError("target appears in more than one collection shard")
        payload = self.model_dump(mode="json", exclude={"manifest_sha256"})
        if self.manifest_sha256 != sha256_json(payload):
            raise ValueError("collection shard manifest hash is invalid")
        return self


def build_production_registry(
    *,
    universe: dict[str, Any],
    health: dict[str, Any],
    target_universe_git_blob_sha: str,
    registry_id: str,
) -> ProductionSourceRegistry:
    """Approve only targets with complete, matching, active health evidence."""
    records = universe.get("target_records")
    results = health.get("results")
    if not isinstance(records, list) or not isinstance(results, list):
        raise ValueError("canonical universe and health results are required")

    by_identity = {}
    for record in records:
        identity = record["target_identity"]
        if identity in by_identity:
            raise ValueError(f"duplicate universe target: {identity}")
        by_identity[identity] = record

    health_by_identity = {}
    for result in results:
        identity = result["target_identity"]
        if identity in health_by_identity:
            raise ValueError(f"duplicate health result: {identity}")
        health_by_identity[identity] = result

    if set(by_identity) != set(health_by_identity):
        missing = sorted(set(by_identity) - set(health_by_identity))
        extra = sorted(set(health_by_identity) - set(by_identity))
        raise ValueError(
            f"health/universe target mismatch: missing={len(missing)} extra={len(extra)}"
        )

    manifest_hash = health.get("manifest_sha256")
    updated_at = health.get("updated_at")
    if not isinstance(manifest_hash, str) or len(manifest_hash) != 64 or not updated_at:
        raise ValueError("complete health manifest provenance is required")

    targets: list[ProductionTarget] = []
    for identity in sorted(by_identity):
        record, result = by_identity[identity], health_by_identity[identity]
        if record["source"] != result["source"]:
            raise ValueError(f"health source mismatch for {identity}")
        if result.get("manifest_sha256") != manifest_hash:
            raise ValueError(f"health manifest mismatch for {identity}")
        if result.get("classification") != "active":
            continue
        targets.append(
            ProductionTarget(
                target_identity=identity,
                source=record["source"],
                coordinates=record["coordinates"],
                company_hint=record.get("company_hint"),
                health_current_postings=result.get("current_postings"),
                health_inventory_exact=result.get("inventory_exact"),
            )
        )

    payload = {
        "registry_version": "production-source-registry-v1",
        "registry_id": registry_id,
        "target_universe_git_blob_sha": target_universe_git_blob_sha,
        "health_manifest_sha256": manifest_hash,
        "health_evidence_updated_at": updated_at,
        "approval_policy": "health-active-only",
        "target_counts_by_source": dict(sorted(Counter(t.source for t in targets).items())),
        "targets": [target.model_dump(mode="json") for target in targets],
    }
    return ProductionSourceRegistry(**payload, registry_sha256=sha256_json(payload))


def build_shard_manifest(
    registry: ProductionSourceRegistry,
    *,
    shard_counts_by_source: dict[str, int],
) -> CollectionShardManifest:
    """Partition each provider independently and deterministically."""
    expected_sources = {target.source for target in registry.targets}
    if set(shard_counts_by_source) != expected_sources:
        raise ValueError("shard counts must be specified for every registry provider")
    if any(value < 1 for value in shard_counts_by_source.values()):
        raise ValueError("shard counts must be positive")

    shards: list[CollectionShard] = []
    for source in PROVIDERS:
        targets = [target for target in registry.targets if target.source == source]
        if not targets:
            continue
        count = shard_counts_by_source[source]
        buckets: list[list[str]] = [[] for _ in range(count)]
        ordered = sorted(
            targets,
            key=lambda target: (
                hashlib.sha256(target.target_identity.encode()).hexdigest(),
                target.target_identity,
            ),
        )
        for index, target in enumerate(ordered):
            buckets[index % count].append(target.target_identity)
        for index, identities in enumerate(buckets):
            shards.append(
                CollectionShard(
                    shard_id=f"{source}-{index:03d}",
                    source=source,
                    target_identities=sorted(identities),
                )
            )

    target_counts = dict(sorted(Counter(t.source for t in registry.targets).items()))
    payload = {
        "manifest_version": "collection-shards-v1",
        "registry_id": registry.registry_id,
        "registry_sha256": registry.registry_sha256,
        "shard_counts_by_source": dict(sorted(shard_counts_by_source.items())),
        "target_counts_by_source": target_counts,
        "shards": [shard.model_dump(mode="json") for shard in shards],
    }
    return CollectionShardManifest(**payload, manifest_sha256=sha256_json(payload))


def load_production_registry(path) -> ProductionSourceRegistry:
    return ProductionSourceRegistry.model_validate_json(path.read_text(encoding="utf-8"))
