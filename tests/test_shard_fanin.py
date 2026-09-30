from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from job_scout.domain.models import CollectionResult, CollectionStatus, Job, SourceTarget
from job_scout.production_registry import build_production_registry, build_shard_manifest
from job_scout.shard_collection import ShardCollectionArtifact, collect_shard
from job_scout.shard_fanin import persist_shard_artifacts
from job_scout.storage.inventory_runs import InventoryRunStore
from job_scout.storage.sqlite import SQLiteRepository

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
PERSISTED = datetime(2026, 9, 30, 12, 5, tzinfo=UTC)


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
    target: SourceTarget,
    *,
    suffix: str = "",
    board: str | None = None,
) -> Job:
    source_id = f"job-{target.board_id}{suffix}"
    return Job(
        id=source_id,
        source="greenhouse",
        source_job_id=source_id,
        source_board_id=board or target.board_id,
        title="Software Engineer",
        company=target.company,
        description_text="Build reliable software systems.",
        job_url=f"https://example.test/{target.board_id}/{source_id}",
        canonical_url=f"https://example.test/{target.board_id}/{source_id}",
        country="United States",
        eligible_countries={"United States"},
        posted_at=NOW - timedelta(hours=2),
        discovered_at=NOW,
        last_seen_at=NOW,
        content_fingerprint=f"fp-{source_id}",
    )


def _artifact(
    registry,
    manifest,
    shard_id: str,
    *,
    duplicate_jobs: bool = False,
    partial_board: str | None = None,
    wrong_board: bool = False,
) -> ShardCollectionArtifact:
    class Collector:
        client = None

        def __init__(self, source: str) -> None:
            self.source = source

        def collect(self, target: SourceTarget) -> CollectionResult:
            job = _job(target, board="wrong" if wrong_board else None)
            jobs = [job, job.model_copy(deep=True)] if duplicate_jobs else [job]
            partial = target.board_id == partial_board
            return CollectionResult(
                source=self.source,
                target=target,
                status=CollectionStatus.PARTIAL if partial else CollectionStatus.SUCCESS,
                jobs=jobs,
                errors=["one provider row was rejected"] if partial else [],
                raw_postings_received=len(jobs) + (1 if partial else 0),
            )

    return collect_shard(
        registry=registry,
        manifest=manifest,
        shard_id=shard_id,
        collector_factory=Collector,
        now=lambda: NOW,
        monotonic=lambda: 0.0,
    )


def _inventory_table_exists(repository: SQLiteRepository) -> bool:
    with repository.connect() as connection:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='inventory_runs'"
            ).fetchone()
            is not None
        )


def test_fan_in_persists_once_and_replays_without_mutating_artifacts(tmp_path: Path) -> None:
    registry = _registry()
    manifest = build_shard_manifest(registry, shard_counts_by_source={"greenhouse": 2})
    artifacts = [
        _artifact(registry, manifest, "greenhouse-000", duplicate_jobs=True),
        _artifact(registry, manifest, "greenhouse-001"),
    ]
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")

    first = persist_shard_artifacts(
        repository=repository,
        registry=registry,
        manifest=manifest,
        artifacts=artifacts,
        now=lambda: PERSISTED,
    )

    assert first.status == "success"
    assert first.replayed is False
    assert first.metrics.targets_attempted == 2
    assert first.metrics.raw_postings_received == 3
    assert first.metrics.normalized_job_records == 3
    assert first.metrics.unique_normalized_jobs == 2
    assert first.metrics.duplicate_job_records_suppressed == 1
    assert first.metrics.inventory_memberships == 2

    store = InventoryRunStore(repository)
    assert len(store.active_job_ids(first.run_id)) == 2
    assert store.get(first.run_id) is not None
    assert store.get(first.run_id).status == "success"

    with repository.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM inventory_run_jobs WHERE run_id=?",
                (first.run_id,),
            ).fetchone()[0]
            == 2
        )

    second = persist_shard_artifacts(
        repository=repository,
        registry=registry,
        manifest=manifest,
        artifacts=artifacts,
        now=lambda: PERSISTED + timedelta(hours=1),
    )

    assert second.run_id == first.run_id
    assert second.replayed is True
    assert second.persisted_at == first.persisted_at
    ShardCollectionArtifact.model_validate(artifacts[0].model_dump(mode="json"))

    with repository.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM inventory_run_jobs WHERE run_id=?",
                (first.run_id,),
            ).fetchone()[0]
            == 2
        )


def test_fan_in_fails_closed_before_inventory_mutation_for_missing_shard(
    tmp_path: Path,
) -> None:
    registry = _registry()
    manifest = build_shard_manifest(registry, shard_counts_by_source={"greenhouse": 2})
    artifact = _artifact(registry, manifest, "greenhouse-000")
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")

    with pytest.raises(ValueError, match="incomplete"):
        persist_shard_artifacts(
            repository=repository,
            registry=registry,
            manifest=manifest,
            artifacts=[artifact],
        )

    assert _inventory_table_exists(repository) is False
    with repository.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_fan_in_fails_closed_on_job_target_provenance_before_mutation(
    tmp_path: Path,
) -> None:
    registry = _registry()
    manifest = build_shard_manifest(registry, shard_counts_by_source={"greenhouse": 1})
    artifact = _artifact(
        registry,
        manifest,
        "greenhouse-000",
        wrong_board=True,
    )
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")

    with pytest.raises(ValueError, match="job provenance"):
        persist_shard_artifacts(
            repository=repository,
            registry=registry,
            manifest=manifest,
            artifacts=[artifact],
        )

    assert _inventory_table_exists(repository) is False
    with repository.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_fan_in_marks_complete_artifact_set_partial_when_one_target_is_partial(
    tmp_path: Path,
) -> None:
    registry = _registry()
    manifest = build_shard_manifest(registry, shard_counts_by_source={"greenhouse": 2})
    artifacts = [
        _artifact(registry, manifest, "greenhouse-000", partial_board="a"),
        _artifact(registry, manifest, "greenhouse-001", partial_board="a"),
    ]
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")

    report = persist_shard_artifacts(
        repository=repository,
        registry=registry,
        manifest=manifest,
        artifacts=artifacts,
        now=lambda: PERSISTED,
    )

    assert report.status == "partial"
    assert report.metrics.targets_succeeded == 1
    assert report.metrics.targets_partial == 1
    assert report.metrics.targets_failed == 0
    assert InventoryRunStore(repository).get(report.run_id).status == "partial"


def test_fan_in_resumes_running_receipt_after_persistence_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    manifest = build_shard_manifest(registry, shard_counts_by_source={"greenhouse": 2})
    artifacts = [
        _artifact(registry, manifest, "greenhouse-000"),
        _artifact(registry, manifest, "greenhouse-001"),
    ]
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")
    original_upsert = repository.upsert_jobs

    def fail_once(jobs) -> dict[str, object]:
        raise RuntimeError("simulated persistence failure")

    with monkeypatch.context() as patch:
        patch.setattr(repository, "upsert_jobs", fail_once)
        with pytest.raises(RuntimeError, match="simulated persistence failure"):
            persist_shard_artifacts(
                repository=repository,
                registry=registry,
                manifest=manifest,
                artifacts=artifacts,
                now=lambda: PERSISTED,
            )

    with repository.connect() as connection:
        row = connection.execute(
            "SELECT run_id,status FROM inventory_runs ORDER BY run_id"
        ).fetchone()
    assert row is not None
    assert row["status"] == "running"

    monkeypatch.setattr(repository, "upsert_jobs", original_upsert)
    resumed = persist_shard_artifacts(
        repository=repository,
        registry=registry,
        manifest=manifest,
        artifacts=artifacts,
        now=lambda: PERSISTED,
    )

    assert resumed.replayed is False
    assert resumed.run_id == row["run_id"]
    assert resumed.status == "success"
    assert InventoryRunStore(repository).get(resumed.run_id).status == "success"


def test_fan_in_reverifies_artifact_hash_before_inventory_mutation(
    tmp_path: Path,
) -> None:
    registry = _registry()
    manifest = build_shard_manifest(registry, shard_counts_by_source={"greenhouse": 2})
    artifacts = [
        _artifact(registry, manifest, "greenhouse-000"),
        _artifact(registry, manifest, "greenhouse-001"),
    ]
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")

    artifacts[0].targets[0].jobs[0].title = "Tampered after validation"

    with pytest.raises(ValueError, match="artifact hash"):
        persist_shard_artifacts(
            repository=repository,
            registry=registry,
            manifest=manifest,
            artifacts=artifacts,
        )

    assert _inventory_table_exists(repository) is False
    with repository.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
