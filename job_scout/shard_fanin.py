"""Authoritative fan-in persistence for validated shard collection artifacts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field

from job_scout.domain.models import CollectionStatus, Job
from job_scout.production_registry import (
    CollectionShardManifest,
    ProductionSourceRegistry,
    sha256_json,
)
from job_scout.shard_collection import (
    ShardCollectionArtifact,
    validate_complete_artifact_set,
)
from job_scout.storage.inventory_runs import InventoryRunStore
from job_scout.storage.sqlite import SQLiteRepository

StorageIdentity = tuple[str, str, str]


def utc_now() -> datetime:
    return datetime.now(UTC)


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


def _job_payload(job: Job) -> dict[str, object]:
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


def _target_observation_rows(
    artifacts: list[ShardCollectionArtifact],
) -> list[tuple[str, str, str, datetime, datetime, int, int, int, int, int]]:
    rows = []
    for artifact in artifacts:
        for target in artifact.targets:
            rows.append(
                (
                    target.target_identity,
                    target.source,
                    target.status.value,
                    target.started_at,
                    target.completed_at,
                    target.runtime_ms,
                    target.raw_postings_received,
                    len(target.jobs),
                    sum(job.posted_at is not None for job in target.jobs),
                    sum(_fresh_24h(job, artifact.completed_at) for job in target.jobs),
                )
            )
    return rows


class FanInMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targets_attempted: int = Field(ge=0)
    targets_succeeded: int = Field(ge=0)
    targets_partial: int = Field(ge=0)
    targets_failed: int = Field(ge=0)
    raw_postings_received: int = Field(ge=0)
    normalized_job_records: int = Field(ge=0)
    unique_normalized_jobs: int = Field(ge=0)
    duplicate_job_records_suppressed: int = Field(ge=0)
    postings_with_trustworthy_timestamps: int = Field(ge=0)
    postings_at_most_24h_old_at_collection: int = Field(ge=0)
    inventory_memberships: int = Field(ge=0)
    target_runtime_p50_ms: int = Field(ge=0)
    target_runtime_p95_ms: int = Field(ge=0)


class FanInReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fan_in_version: Literal["artifact-fan-in-v1"] = "artifact-fan-in-v1"
    run_id: str
    inventory_plan_id: str
    registry_id: str
    registry_sha256: str
    shard_manifest_sha256: str
    artifact_set_sha256: str
    collection_started_at: datetime
    collection_completed_at: datetime
    persisted_at: datetime
    status: Literal["success", "partial", "failure"]
    replayed: bool
    metrics: FanInMetrics


def _ordered_artifacts(
    manifest: CollectionShardManifest,
    artifacts: list[ShardCollectionArtifact],
) -> list[ShardCollectionArtifact]:
    by_id = {artifact.shard_id: artifact for artifact in artifacts}
    return [by_id[shard.shard_id] for shard in manifest.shards]


def _artifact_set_sha(artifacts: list[ShardCollectionArtifact]) -> str:
    return sha256_json(
        [
            {
                "shard_id": artifact.shard_id,
                "artifact_sha256": artifact.artifact_sha256,
            }
            for artifact in sorted(artifacts, key=lambda value: value.shard_id)
        ]
    )


def _prepare_jobs(
    *,
    registry: ProductionSourceRegistry,
    artifacts: list[ShardCollectionArtifact],
) -> tuple[
    dict[StorageIdentity, Job],
    list[tuple[str, StorageIdentity]],
    int,
]:
    targets = {target.target_identity: target for target in registry.targets}
    jobs_by_identity: dict[StorageIdentity, Job] = {}
    hashes_by_identity: dict[StorageIdentity, str] = {}
    job_ids: dict[str, StorageIdentity] = {}
    memberships: list[tuple[str, StorageIdentity]] = []
    records = 0

    for artifact in artifacts:
        for target_result in artifact.targets:
            production_target = targets[target_result.target_identity]
            expected = production_target.source_target()
            for job in target_result.jobs:
                records += 1
                if job.source != target_result.source or job.source_board_id != expected.board_id:
                    raise ValueError(
                        f"job provenance does not match target: {target_result.target_identity}"
                    )
                # company_hint is discovery/display metadata, not provider identity.
                # Providers may return a legal/hiring-organization name that differs
                # from the configured hint. Provenance is bound by source + board
                # + provider job identity and the validated shard target membership.
                identity = (job.source, job.source_board_id, job.source_job_id)
                payload_hash = sha256_json(_job_payload(job))
                previous_hash = hashes_by_identity.get(identity)
                if previous_hash is not None and previous_hash != payload_hash:
                    raise ValueError("conflicting duplicate normalized job identity")
                previous_identity = job_ids.get(job.id)
                if previous_identity is not None and previous_identity != identity:
                    raise ValueError("normalized job id collides across provider identities")
                hashes_by_identity[identity] = payload_hash
                job_ids[job.id] = identity
                jobs_by_identity.setdefault(identity, job.model_copy(deep=True))
                memberships.append((target_result.target_identity, identity))

    return jobs_by_identity, memberships, records


def _metrics(
    *,
    artifacts: list[ShardCollectionArtifact],
    normalized_job_records: int,
    unique_normalized_jobs: int,
    inventory_memberships: int,
) -> FanInMetrics:
    targets = [target for artifact in artifacts for target in artifact.targets]
    succeeded = sum(target.status is CollectionStatus.SUCCESS for target in targets)
    partial = sum(target.status is CollectionStatus.PARTIAL for target in targets)
    failed = len(targets) - succeeded - partial
    runtimes = [target.runtime_ms for target in targets]
    return FanInMetrics(
        targets_attempted=len(targets),
        targets_succeeded=succeeded,
        targets_partial=partial,
        targets_failed=failed,
        raw_postings_received=sum(artifact.metrics.raw_postings_received for artifact in artifacts),
        normalized_job_records=normalized_job_records,
        unique_normalized_jobs=unique_normalized_jobs,
        duplicate_job_records_suppressed=normalized_job_records - unique_normalized_jobs,
        postings_with_trustworthy_timestamps=sum(
            artifact.metrics.postings_with_trustworthy_timestamps for artifact in artifacts
        ),
        postings_at_most_24h_old_at_collection=sum(
            artifact.metrics.postings_at_most_24h_old_at_collection for artifact in artifacts
        ),
        inventory_memberships=inventory_memberships,
        target_runtime_p50_ms=_percentile(runtimes, 0.50),
        target_runtime_p95_ms=_percentile(runtimes, 0.95),
    )


def _status(metrics: FanInMetrics) -> Literal["success", "partial", "failure"]:
    if metrics.targets_failed == 0 and metrics.targets_partial == 0:
        return "success"
    if metrics.targets_succeeded or metrics.targets_partial:
        return "partial"
    return "failure"


def persist_shard_artifacts(
    *,
    repository: SQLiteRepository,
    registry: ProductionSourceRegistry,
    manifest: CollectionShardManifest,
    artifacts: list[ShardCollectionArtifact],
    payload_retention_hours: int | None = None,
    now: Callable[[], datetime] = utc_now,
) -> FanInReport:
    """Validate the complete artifact set before making any inventory-run mutation."""
    if not artifacts:
        raise ValueError("fan-in requires at least one shard artifact")

    # Re-parse every artifact immediately before persistence so in-memory mutation
    # after initial loading cannot bypass the artifact SHA validator.
    verified_artifacts = [
        ShardCollectionArtifact.model_validate(artifact.model_dump(mode="json"))
        for artifact in artifacts
    ]
    validate_complete_artifact_set(
        registry=registry,
        manifest=manifest,
        artifacts=verified_artifacts,
    )

    ordered = _ordered_artifacts(manifest, verified_artifacts)
    jobs_by_identity, membership_keys, normalized_records = _prepare_jobs(
        registry=registry,
        artifacts=ordered,
    )
    distinct_memberships = list(dict.fromkeys(membership_keys))
    retained_identities = set(jobs_by_identity)
    stale_jobs: list[Job] = []
    retention_evaluated_at: datetime | None = None
    if payload_retention_hours is not None:
        if payload_retention_hours < 1:
            raise ValueError("payload_retention_hours must be at least 1")
        retention_evaluated_at = now()
        retention_evaluated_at = retention_evaluated_at.replace(
            tzinfo=retention_evaluated_at.tzinfo or UTC
        ).astimezone(UTC)
        cutoff = retention_evaluated_at - timedelta(hours=payload_retention_hours)
        stale_identities = {
            identity
            for identity, job in jobs_by_identity.items()
            if job.posted_at is not None
            and job.posted_at.replace(tzinfo=job.posted_at.tzinfo or UTC).astimezone(UTC)
            < cutoff
        }
        stale_jobs = [jobs_by_identity[identity] for identity in stale_identities]
        retained_identities -= stale_identities

    retained_memberships = [
        membership
        for membership in distinct_memberships
        if membership[1] in retained_identities
    ]
    metrics = _metrics(
        artifacts=ordered,
        normalized_job_records=normalized_records,
        unique_normalized_jobs=len(jobs_by_identity),
        inventory_memberships=len(retained_memberships),
    )
    status = _status(metrics)
    artifact_set_sha = _artifact_set_sha(ordered)
    retention_identity = (
        "none" if payload_retention_hours is None else str(payload_retention_hours)
    )
    run_id = str(
        uuid5(
            NAMESPACE_URL,
            "inventory-fanin:"
            f"{registry.registry_id}:{manifest.manifest_sha256}:{artifact_set_sha}:"
            f"retention-hours={retention_identity}",
        )
    )
    registry_sha = sha256_json(registry.model_dump(mode="json"))
    collection_started_at = min(artifact.started_at for artifact in ordered)
    collection_completed_at = max(artifact.completed_at for artifact in ordered)

    inventory = InventoryRunStore(repository)
    created = inventory.create_if_absent(
        run_id=run_id,
        plan_id=registry.registry_id,
        started_at=collection_started_at,
    )
    existing = inventory.get(run_id)
    if existing is None:
        raise RuntimeError("inventory run receipt disappeared")

    if not created and existing.status != "running":
        if existing.completed_at is None:
            raise ValueError("completed inventory run is missing completion time")
        replay_metrics = metrics.model_copy(
            update={"inventory_memberships": inventory.membership_count(run_id)}
        )
        return FanInReport(
            run_id=run_id,
            inventory_plan_id=registry.registry_id,
            registry_id=registry.registry_id,
            registry_sha256=registry_sha,
            shard_manifest_sha256=manifest.manifest_sha256,
            artifact_set_sha256=artifact_set_sha,
            collection_started_at=collection_started_at,
            collection_completed_at=collection_completed_at,
            persisted_at=existing.completed_at,
            status=existing.status,
            replayed=True,
            metrics=replay_metrics,
        )

    jobs = [
        jobs_by_identity[identity]
        for identity in sorted(retained_identities)
    ]
    membership_jobs = [
        (target_identity, jobs_by_identity[identity])
        for target_identity, identity in retained_memberships
    ]
    persisted_at = now()
    inventory.persist_jobs_and_finish(
        run_id=run_id,
        jobs=jobs,
        memberships=membership_jobs,
        stale_jobs=stale_jobs,
        target_observations=_target_observation_rows(ordered),
        pruned_at=retention_evaluated_at,
        status=status,
        completed_at=persisted_at,
    )

    return FanInReport(
        run_id=run_id,
        inventory_plan_id=registry.registry_id,
        registry_id=registry.registry_id,
        registry_sha256=registry_sha,
        shard_manifest_sha256=manifest.manifest_sha256,
        artifact_set_sha256=artifact_set_sha,
        collection_started_at=collection_started_at,
        collection_completed_at=collection_completed_at,
        persisted_at=persisted_at,
        status=status,
        replayed=False,
        metrics=metrics,
    )
