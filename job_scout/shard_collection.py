"""Pure shard collection: network reads in workers, normalized artifacts out."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from job_scout.collectors.ashby import AshbyCollector
from job_scout.collectors.base import JobCollector
from job_scout.collectors.greenhouse import GreenhouseCollector
from job_scout.collectors.lever import LeverCollector
from job_scout.collectors.smartrecruiters import SmartRecruitersCollector
from job_scout.collectors.workday import WorkdayCollector
from job_scout.domain.models import CollectionStatus, Job
from job_scout.production_registry import (
    CollectionShardManifest,
    ProductionSourceRegistry,
    ProductionTarget,
    sha256_json,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def default_collector_factory(source: str) -> JobCollector:
    return {
        "greenhouse": GreenhouseCollector,
        "ashby": AshbyCollector,
        "workday": WorkdayCollector,
        "lever": LeverCollector,
        "smartrecruiters": SmartRecruitersCollector,
    }[source]()


def _close_collector(collector: JobCollector) -> None:
    client = getattr(collector, "client", None)
    if client is not None:
        client.close()


def _percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def _json_datetime(value: datetime) -> str:
    normalized = value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)
    return normalized.isoformat().replace("+00:00", "Z")


def _job_json(job: Job) -> dict[str, object]:
    payload = job.model_dump(mode="json")
    payload["eligible_countries"] = sorted(job.eligible_countries)
    return payload


def _fresh_24h(job: Job, evaluated_at: datetime) -> bool:
    if job.posted_at is None:
        return False
    posted_at = job.posted_at.replace(tzinfo=job.posted_at.tzinfo or UTC).astimezone(UTC)
    evaluated_at = evaluated_at.replace(tzinfo=evaluated_at.tzinfo or UTC).astimezone(UTC)
    age_seconds = (evaluated_at - posted_at).total_seconds()
    return -300 <= age_seconds <= 24 * 3600


class ShardTargetResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_identity: str
    source: Literal["greenhouse", "ashby", "workday", "lever", "smartrecruiters"]
    status: CollectionStatus
    started_at: datetime
    completed_at: datetime
    runtime_ms: int = Field(ge=0)
    raw_postings_received: int = Field(ge=0)
    jobs: list[Job] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    telemetry: dict[str, int | float | str | bool | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_result(self) -> ShardTargetResult:
        if self.completed_at < self.started_at:
            raise ValueError("target completion precedes start")
        if self.status is not CollectionStatus.SUCCESS and not self.errors:
            raise ValueError("non-success target collection requires an error")
        if self.raw_postings_received < len(self.jobs):
            raise ValueError("raw posting count cannot be lower than normalized jobs")
        return self


def _target_json(target: ShardTargetResult) -> dict[str, object]:
    payload = target.model_dump(mode="json")
    payload["jobs"] = [_job_json(job) for job in target.jobs]
    return payload


class ShardCollectionMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targets_attempted: int = Field(ge=0)
    targets_succeeded: int = Field(ge=0)
    targets_partial: int = Field(ge=0)
    targets_failed: int = Field(ge=0)
    raw_postings_received: int = Field(ge=0)
    postings_with_trustworthy_timestamps: int = Field(ge=0)
    postings_at_most_24h_old_at_collection: int = Field(ge=0)
    target_runtime_p50_ms: int = Field(ge=0)
    target_runtime_p95_ms: int = Field(ge=0)


class ShardCollectionArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_version: Literal["collection-shard-artifact-v1"] = "collection-shard-artifact-v1"
    registry_id: str
    registry_sha256: str
    shard_manifest_sha256: str
    shard_id: str
    source: Literal["greenhouse", "ashby", "workday", "lever", "smartrecruiters"]
    started_at: datetime
    completed_at: datetime
    metrics: ShardCollectionMetrics
    targets: list[ShardTargetResult]
    artifact_sha256: str

    @model_validator(mode="after")
    def validate_artifact(self) -> ShardCollectionArtifact:
        if self.completed_at < self.started_at:
            raise ValueError("shard completion precedes start")
        if any(target.source != self.source for target in self.targets):
            raise ValueError("shard artifact mixes providers")
        if len({target.target_identity for target in self.targets}) != len(self.targets):
            raise ValueError("shard artifact contains duplicate targets")

        succeeded = sum(target.status is CollectionStatus.SUCCESS for target in self.targets)
        partial = sum(target.status is CollectionStatus.PARTIAL for target in self.targets)
        failed = len(self.targets) - succeeded - partial
        jobs = [job for target in self.targets for job in target.jobs]
        runtimes = [target.runtime_ms for target in self.targets]
        expected = ShardCollectionMetrics(
            targets_attempted=len(self.targets),
            targets_succeeded=succeeded,
            targets_partial=partial,
            targets_failed=failed,
            raw_postings_received=sum(target.raw_postings_received for target in self.targets),
            postings_with_trustworthy_timestamps=sum(job.posted_at is not None for job in jobs),
            postings_at_most_24h_old_at_collection=sum(
                _fresh_24h(job, self.completed_at) for job in jobs
            ),
            target_runtime_p50_ms=_percentile(runtimes, 0.50),
            target_runtime_p95_ms=_percentile(runtimes, 0.95),
        )
        if self.metrics != expected:
            raise ValueError("shard collection metrics do not reconcile")
        payload = self.model_dump(mode="json", exclude={"artifact_sha256"})
        payload["targets"] = [_target_json(target) for target in self.targets]
        if self.artifact_sha256 != sha256_json(payload):
            raise ValueError("shard collection artifact hash is invalid")
        return self


def _build_artifact(
    *,
    registry: ProductionSourceRegistry,
    manifest: CollectionShardManifest,
    shard_id: str,
    source: str,
    started_at: datetime,
    completed_at: datetime,
    targets: list[ShardTargetResult],
) -> ShardCollectionArtifact:
    succeeded = sum(target.status is CollectionStatus.SUCCESS for target in targets)
    partial = sum(target.status is CollectionStatus.PARTIAL for target in targets)
    failed = len(targets) - succeeded - partial
    jobs = [job for target in targets for job in target.jobs]
    runtimes = [target.runtime_ms for target in targets]
    metrics = ShardCollectionMetrics(
        targets_attempted=len(targets),
        targets_succeeded=succeeded,
        targets_partial=partial,
        targets_failed=failed,
        raw_postings_received=sum(target.raw_postings_received for target in targets),
        postings_with_trustworthy_timestamps=sum(job.posted_at is not None for job in jobs),
        postings_at_most_24h_old_at_collection=sum(_fresh_24h(job, completed_at) for job in jobs),
        target_runtime_p50_ms=_percentile(runtimes, 0.50),
        target_runtime_p95_ms=_percentile(runtimes, 0.95),
    )
    payload = {
        "artifact_version": "collection-shard-artifact-v1",
        "registry_id": registry.registry_id,
        "registry_sha256": sha256_json(registry.model_dump(mode="json")),
        "shard_manifest_sha256": manifest.manifest_sha256,
        "shard_id": shard_id,
        "source": source,
        "started_at": _json_datetime(started_at),
        "completed_at": _json_datetime(completed_at),
        "metrics": metrics.model_dump(mode="json"),
        "targets": [_target_json(target) for target in targets],
    }
    return ShardCollectionArtifact(**payload, artifact_sha256=sha256_json(payload))


def collect_shard(
    *,
    registry: ProductionSourceRegistry,
    manifest: CollectionShardManifest,
    shard_id: str,
    collector_factory: Callable[[str], JobCollector] = default_collector_factory,
    now: Callable[[], datetime] = utc_now,
    monotonic: Callable[[], float] = time.perf_counter,
) -> ShardCollectionArtifact:
    """Collect exactly one provider shard and return a DB-free normalized artifact."""
    registry_sha = sha256_json(registry.model_dump(mode="json"))
    if manifest.registry_id != registry.registry_id or manifest.registry_sha256 != registry_sha:
        raise ValueError("shard manifest does not match production registry")

    shards = [shard for shard in manifest.shards if shard.shard_id == shard_id]
    if len(shards) != 1:
        raise ValueError("requested shard id is not unique in manifest")
    shard = shards[0]
    targets_by_id: dict[str, ProductionTarget] = {
        target.target_identity: target for target in registry.targets
    }
    if any(identity not in targets_by_id for identity in shard.target_identities):
        raise ValueError("shard contains target outside production registry")

    shard_started = now()
    results: list[ShardTargetResult] = []
    for identity in shard.target_identities:
        target = targets_by_id[identity]
        if target.source != shard.source:
            raise ValueError("shard target provider does not match shard provider")
        started_at = now()
        timer_start = monotonic()
        collector = collector_factory(shard.source)
        try:
            result = collector.collect(target.source_target())
            completed_at = now()
            runtime_ms = max(0, round((monotonic() - timer_start) * 1000))
            raw_postings_received = result.raw_postings_received
            if raw_postings_received is None:
                if result.status in {CollectionStatus.SUCCESS, CollectionStatus.PARTIAL}:
                    raise ValueError("collector did not report raw provider posting count")
                raw_postings_received = 0
            telemetry = getattr(collector, "last_counts", {})
            if not isinstance(telemetry, dict):
                raise TypeError("collector telemetry must be a dictionary")
            results.append(
                ShardTargetResult(
                    target_identity=identity,
                    source=shard.source,
                    status=result.status,
                    started_at=started_at,
                    completed_at=completed_at,
                    runtime_ms=runtime_ms,
                    raw_postings_received=raw_postings_received,
                    jobs=result.jobs,
                    errors=result.errors,
                    telemetry=dict(telemetry),
                )
            )
        finally:
            _close_collector(collector)

    completed_at = now()
    artifact = _build_artifact(
        registry=registry,
        manifest=manifest,
        shard_id=shard.shard_id,
        source=shard.source,
        started_at=shard_started,
        completed_at=completed_at,
        targets=results,
    )
    expected = shard.target_identities
    actual = [target.target_identity for target in artifact.targets]
    if actual != expected:
        raise ValueError("shard artifact target coverage/order does not match manifest")
    return artifact


def validate_complete_artifact_set(
    *,
    registry: ProductionSourceRegistry,
    manifest: CollectionShardManifest,
    artifacts: list[ShardCollectionArtifact],
) -> None:
    """Fail closed unless one valid artifact exists for every manifest shard."""
    registry_sha = sha256_json(registry.model_dump(mode="json"))
    if manifest.registry_id != registry.registry_id or manifest.registry_sha256 != registry_sha:
        raise ValueError("shard manifest does not match production registry")
    by_id = {artifact.shard_id: artifact for artifact in artifacts}
    if len(by_id) != len(artifacts):
        raise ValueError("duplicate shard artifact")
    expected_ids = {shard.shard_id for shard in manifest.shards}
    if set(by_id) != expected_ids:
        raise ValueError("shard artifact set is incomplete or contains unknown shards")
    for shard in manifest.shards:
        artifact = by_id[shard.shard_id]
        if (
            artifact.registry_id != registry.registry_id
            or artifact.registry_sha256 != registry_sha
            or artifact.shard_manifest_sha256 != manifest.manifest_sha256
            or artifact.source != shard.source
            or [target.target_identity for target in artifact.targets] != shard.target_identities
        ):
            raise ValueError(f"shard artifact provenance/coverage mismatch: {shard.shard_id}")
