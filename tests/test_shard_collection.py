from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from job_scout.domain.models import (
    CollectionResult,
    CollectionStatus,
    Job,
    SourceTarget,
)
from job_scout.production_registry import (
    build_production_registry,
    build_shard_manifest,
)
from job_scout.shard_collection import (
    ShardCollectionArtifact,
    collect_shard,
    validate_complete_artifact_set,
)


NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _registry():
    universe = {
        "target_records": [
            {
                "target_identity": "greenhouse:a",
                "source": "greenhouse",
                "coordinates": {"board": "a"},
                "company_hint": "Alpha",
            },
            {
                "target_identity": "greenhouse:b",
                "source": "greenhouse",
                "coordinates": {"board": "b"},
                "company_hint": "Beta",
            },
        ]
    }
    health = {
        "manifest_sha256": "a" * 64,
        "updated_at": "2026-09-09T05:13:04+00:00",
        "results": [
            {
                "target_identity": item["target_identity"],
                "source": "greenhouse",
                "classification": "active",
                "current_postings": 2,
                "inventory_exact": True,
                "manifest_sha256": "a" * 64,
            }
            for item in universe["target_records"]
        ],
    }
    return build_production_registry(
        universe=universe,
        health=health,
        target_universe_git_blob_sha="b" * 40,
        registry_id="test-registry",
    )


def _job(
    *,
    job_id: str,
    board: str,
    company: str,
    posted_at: datetime | None,
) -> Job:
    return Job(
        id=job_id,
        source="greenhouse",
        source_job_id=job_id,
        source_board_id=board,
        title="Software Engineer",
        company=company,
        job_url=f"https://example.com/jobs/{job_id}",
        canonical_url=f"https://example.com/jobs/{job_id}",
        posted_at=posted_at,
        discovered_at=NOW,
        last_seen_at=NOW,
        content_fingerprint=f"fp-{job_id}",
    )


class FakeCollector:
    def __init__(self, result: CollectionResult) -> None:
        self.result = result
        self.closed = False

    def collect(self, target: SourceTarget) -> CollectionResult:
        assert target.board_id == self.result.target.board_id
        return self.result


def _clock(values):
    values = iter(values)
    return lambda: next(values)


def test_collect_shard_emits_normalized_metrics_without_persistence() -> None:
    registry = _registry()
    manifest = build_shard_manifest(registry, shard_counts_by_source={"greenhouse": 1})
    by_board = {
        "a": CollectionResult(
            source="greenhouse",
            target=SourceTarget(board_id="a", company="Alpha"),
            status=CollectionStatus.SUCCESS,
            jobs=[
                _job(job_id="fresh", board="a", company="Alpha", posted_at=NOW - timedelta(hours=3)),
                _job(job_id="unknown", board="a", company="Alpha", posted_at=None),
            ],
        ),
        "b": CollectionResult(
            source="greenhouse",
            target=SourceTarget(board_id="b", company="Beta"),
            status=CollectionStatus.PARTIAL,
            jobs=[
                _job(job_id="stale", board="b", company="Beta", posted_at=NOW - timedelta(hours=30))
            ],
            errors=["provider pagination stopped"],
        ),
    }

    class Collector:
        def __init__(self, source: str) -> None:
            assert source == "greenhouse"
            self.client = None

        def collect(self, target: SourceTarget) -> CollectionResult:
            return by_board[target.board_id]

    now = _clock([NOW, NOW, NOW, NOW, NOW, NOW])
    monotonic = _clock([0.0, 0.10, 0.10, 0.35])
    artifact = collect_shard(
        registry=registry,
        manifest=manifest,
        shard_id="greenhouse-000",
        collector_factory=Collector,
        now=now,
        monotonic=monotonic,
    )

    assert artifact.metrics.targets_attempted == 2
    assert artifact.metrics.targets_succeeded == 1
    assert artifact.metrics.targets_partial == 1
    assert artifact.metrics.targets_failed == 0
    assert artifact.metrics.raw_postings_received == 3
    assert artifact.metrics.postings_with_trustworthy_timestamps == 2
    assert artifact.metrics.postings_at_most_24h_old_at_collection == 1
    assert artifact.metrics.target_runtime_p50_ms == 175
    assert artifact.metrics.target_runtime_p95_ms == 242
    assert artifact.artifact_sha256


def test_artifact_hash_rejects_tampering() -> None:
    registry = _registry()
    manifest = build_shard_manifest(registry, shard_counts_by_source={"greenhouse": 1})

    class Collector:
        client = None

        def __init__(self, source: str) -> None:
            self.source = source

        def collect(self, target: SourceTarget) -> CollectionResult:
            return CollectionResult(
                source=self.source,
                target=target,
                status=CollectionStatus.SUCCESS,
                jobs=[],
            )

    artifact = collect_shard(
        registry=registry,
        manifest=manifest,
        shard_id="greenhouse-000",
        collector_factory=Collector,
        now=_clock([NOW, NOW, NOW, NOW, NOW, NOW]),
        monotonic=_clock([0.0, 0.01, 0.01, 0.02]),
    )
    payload = artifact.model_dump(mode="json")
    payload["metrics"]["raw_postings_received"] = 99
    with pytest.raises(ValueError):
        ShardCollectionArtifact.model_validate(payload)


def test_complete_artifact_set_fails_closed_on_missing_or_duplicate_shards() -> None:
    registry = _registry()
    manifest = build_shard_manifest(registry, shard_counts_by_source={"greenhouse": 2})

    class Collector:
        client = None

        def __init__(self, source: str) -> None:
            self.source = source

        def collect(self, target: SourceTarget) -> CollectionResult:
            return CollectionResult(
                source=self.source,
                target=target,
                status=CollectionStatus.SUCCESS,
                jobs=[],
            )

    artifacts = []
    for shard_id in ("greenhouse-000", "greenhouse-001"):
        artifacts.append(
            collect_shard(
                registry=registry,
                manifest=manifest,
                shard_id=shard_id,
                collector_factory=Collector,
                now=_clock([NOW, NOW, NOW, NOW]),
                monotonic=_clock([0.0, 0.01]),
            )
        )

    validate_complete_artifact_set(registry=registry, manifest=manifest, artifacts=artifacts)

    with pytest.raises(ValueError, match="incomplete"):
        validate_complete_artifact_set(
            registry=registry,
            manifest=manifest,
            artifacts=artifacts[:1],
        )
    with pytest.raises(ValueError, match="duplicate"):
        validate_complete_artifact_set(
            registry=registry,
            manifest=manifest,
            artifacts=[artifacts[0], artifacts[0]],
        )


def test_production_target_requires_company_hint_for_collection() -> None:
    universe = {
        "target_records": [
            {
                "target_identity": "greenhouse:a",
                "source": "greenhouse",
                "coordinates": {"board": "a"},
                "company_hint": None,
            }
        ]
    }
    health = {
        "manifest_sha256": "a" * 64,
        "updated_at": "2026-09-09T05:13:04+00:00",
        "results": [
            {
                "target_identity": "greenhouse:a",
                "source": "greenhouse",
                "classification": "active",
                "current_postings": 1,
                "inventory_exact": True,
                "manifest_sha256": "a" * 64,
            }
        ],
    }
    with pytest.raises(ValueError, match="company hint"):
        build_production_registry(
            universe=universe,
            health=health,
            target_universe_git_blob_sha="b" * 40,
            registry_id="test-registry",
        )
