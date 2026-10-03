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
    company: str | None = None,
) -> Job:
    source_id = f"job-{target.board_id}{suffix}"
    return Job(
        id=source_id,
        source="greenhouse",
        source_job_id=source_id,
        source_board_id=board or target.board_id,
        title="Software Engineer",
        company=company or target.company,
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
    stale_board: str | None = None,
    aging_board: str | None = None,
    wrong_board: bool = False,
    provider_company: str | None = None,
) -> ShardCollectionArtifact:
    class Collector:
        client = None

        def __init__(self, source: str) -> None:
            self.source = source

        def collect(self, target: SourceTarget) -> CollectionResult:
            job = _job(
                target,
                board="wrong" if wrong_board else None,
                company=provider_company,
            )
            if target.board_id == stale_board:
                job = job.model_copy(update={"posted_at": NOW - timedelta(hours=96)})
            elif target.board_id == aging_board:
                job = job.model_copy(update={"posted_at": NOW - timedelta(hours=71)})
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
    assert len(
        store.active_job_ids(
            first.run_id,
            retention_hours=72,
            evaluated_at=first.collection_completed_at,
        )
    ) == 2
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
        observations = connection.execute(
            "SELECT target_identity,status,raw_postings_received,normalized_jobs,"
            "postings_with_trustworthy_timestamps,postings_at_most_24h_old "
            "FROM inventory_target_observations WHERE run_id=? "
            "ORDER BY target_identity",
            (first.run_id,),
        ).fetchall()
        assert len(observations) == 2
        assert [row["status"] for row in observations] == ["success", "success"]
        assert sum(row["raw_postings_received"] for row in observations) == 3
        assert sum(row["normalized_jobs"] for row in observations) == 3
        assert sum(row["postings_with_trustworthy_timestamps"] for row in observations) == 3
        assert sum(row["postings_at_most_24h_old"] for row in observations) == 3

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
        assert connection.execute(
            "SELECT COUNT(*) FROM inventory_target_observations WHERE run_id=?",
            (first.run_id,),
        ).fetchone()[0] == 2


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


def test_fan_in_retention_routes_expired_payloads_directly_to_identity_ledger(
    tmp_path: Path,
) -> None:
    registry = _registry()
    manifest = build_shard_manifest(registry, shard_counts_by_source={"greenhouse": 2})
    artifacts = [
        _artifact(registry, manifest, "greenhouse-000", stale_board="a"),
        _artifact(registry, manifest, "greenhouse-001", stale_board="a"),
    ]
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")

    report = persist_shard_artifacts(
        repository=repository,
        registry=registry,
        manifest=manifest,
        artifacts=artifacts,
        payload_retention_hours=72,
        now=lambda: PERSISTED,
    )

    assert report.status == "success"
    assert report.metrics.normalized_job_records == 2
    assert report.metrics.unique_normalized_jobs == 2
    assert report.metrics.inventory_memberships == 1

    with repository.connect() as connection:
        jobs = connection.execute(
            "SELECT source_job_id,payload_json FROM jobs ORDER BY source_job_id"
        ).fetchall()
        ledger = connection.execute(
            "SELECT source_job_id,pruned_at FROM job_identity_ledger ORDER BY source_job_id"
        ).fetchall()
        memberships = connection.execute(
            "SELECT job_id FROM inventory_run_jobs WHERE run_id=?",
            (report.run_id,),
        ).fetchall()

    assert [row["source_job_id"] for row in jobs] == ["job-b"]
    assert [row["source_job_id"] for row in ledger] == ["job-a"]
    assert ledger[0]["pruned_at"] == PERSISTED.isoformat()
    assert len(memberships) == 1


def test_stale_identity_path_never_downgrades_delivered_history(
    tmp_path: Path,
) -> None:
    registry = _registry()
    target = registry.targets[0].source_target()
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")
    stale = _job(target).model_copy(update={"posted_at": NOW - timedelta(hours=96)})

    with repository.connect() as connection:
        connection.execute(
            "INSERT INTO job_identity_ledger "
            "(source,source_board_id,source_job_id,canonical_url,employer_id,company,"
            "first_seen_at,last_seen_at,pruned_at,was_delivered) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                stale.source,
                stale.source_board_id,
                stale.source_job_id,
                str(stale.canonical_url),
                stale.employer_id,
                stale.company,
                (NOW - timedelta(days=10)).isoformat(),
                (NOW - timedelta(days=1)).isoformat(),
                (NOW - timedelta(days=1)).isoformat(),
                1,
            ),
        )

    repository.record_pruned_identities([stale], pruned_at=PERSISTED)

    with repository.connect() as connection:
        row = connection.execute(
            "SELECT was_delivered FROM job_identity_ledger "
            "WHERE source=? AND source_board_id=? AND source_job_id=?",
            (stale.source, stale.source_board_id, stale.source_job_id),
        ).fetchone()

    assert row is not None
    assert row["was_delivered"] == 1


def test_stale_identity_path_preserves_latest_reobservation_through_prune(
    tmp_path: Path,
) -> None:
    registry = _registry()
    target = registry.targets[0].source_target()
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")
    original_first_seen = NOW - timedelta(days=10)
    stale = _job(target).model_copy(
        update={
            "posted_at": NOW - timedelta(hours=96),
            "discovered_at": original_first_seen,
            "last_seen_at": NOW,
        }
    )
    repository.upsert_job(stale)

    reobserved = stale.model_copy(
        update={"discovered_at": PERSISTED, "last_seen_at": PERSISTED}
    )
    repository.record_pruned_identities([reobserved], pruned_at=PERSISTED)
    repository.prune_stale_inventory(retention_hours=72, now=PERSISTED)

    with repository.connect() as connection:
        row = connection.execute(
            "SELECT first_seen_at,last_seen_at FROM job_identity_ledger "
            "WHERE source=? AND source_board_id=? AND source_job_id=?",
            (stale.source, stale.source_board_id, stale.source_job_id),
        ).fetchone()
        live = connection.execute(
            "SELECT 1 FROM jobs WHERE source=? AND source_board_id=? AND source_job_id=?",
            (stale.source, stale.source_board_id, stale.source_job_id),
        ).fetchone()

    assert row is not None
    assert row["first_seen_at"] == original_first_seen.isoformat()
    assert row["last_seen_at"] == PERSISTED.isoformat()
    assert live is None


def test_incoming_stale_posted_at_prunes_existing_unknown_payload(
    tmp_path: Path,
) -> None:
    registry = _registry()
    manifest = build_shard_manifest(registry, shard_counts_by_source={"greenhouse": 2})
    target = next(
        target for target in registry.targets if target.target_identity == "greenhouse:a"
    )
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")

    stored_unknown = _job(target.source_target()).model_copy(
        update={
            "posted_at": None,
            "job_url": "https://example.test/a/old-job-a",
            "canonical_url": "https://example.test/a/old-job-a",
        }
    )
    repository.upsert_job(stored_unknown)

    artifacts = [
        _artifact(registry, manifest, "greenhouse-000", stale_board="a"),
        _artifact(registry, manifest, "greenhouse-001", stale_board="a"),
    ]
    incoming_url = str(artifacts[0].targets[0].jobs[0].canonical_url)
    persist_shard_artifacts(
        repository=repository,
        registry=registry,
        manifest=manifest,
        artifacts=artifacts,
        payload_retention_hours=72,
        now=lambda: PERSISTED,
    )
    repository.prune_stale_inventory(retention_hours=72, now=PERSISTED)

    with repository.connect() as connection:
        live = connection.execute(
            "SELECT 1 FROM jobs WHERE source=? AND source_board_id=? AND source_job_id=?",
            (
                stored_unknown.source,
                stored_unknown.source_board_id,
                stored_unknown.source_job_id,
            ),
        ).fetchone()
        ledger = connection.execute(
            "SELECT canonical_url,last_seen_at FROM job_identity_ledger "
            "WHERE source=? AND source_board_id=? AND source_job_id=?",
            (
                stored_unknown.source,
                stored_unknown.source_board_id,
                stored_unknown.source_job_id,
            ),
        ).fetchone()

    assert live is None
    assert ledger is not None
    assert ledger["canonical_url"] == incoming_url


def test_fresh_upsert_clears_prior_stale_retention_evidence(
    tmp_path: Path,
) -> None:
    registry = _registry()
    target = registry.targets[0].source_target()
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")
    stored = _job(target)
    repository.upsert_job(stored)

    stale = stored.model_copy(update={"posted_at": NOW - timedelta(hours=96)})
    repository.record_pruned_identities([stale], pruned_at=PERSISTED)

    fresh = stored.model_copy(
        update={
            "posted_at": PERSISTED - timedelta(hours=2),
            "last_seen_at": PERSISTED,
            "content_fingerprint": "fp-fresh-correction",
        }
    )
    repository.upsert_job(fresh)
    repository.prune_stale_inventory(retention_hours=72, now=PERSISTED)

    with repository.connect() as connection:
        live = connection.execute(
            "SELECT payload_json FROM jobs "
            "WHERE source=? AND source_board_id=? AND source_job_id=?",
            (fresh.source, fresh.source_board_id, fresh.source_job_id),
        ).fetchone()
        evidence = connection.execute(
            "SELECT 1 FROM job_retention_evidence WHERE job_id=?",
            (fresh.id,),
        ).fetchone()

    assert live is not None
    assert Job.model_validate_json(live["payload_json"]).posted_at == fresh.posted_at
    assert evidence is None


def test_fan_in_replay_identity_includes_retention_policy(
    tmp_path: Path,
) -> None:
    registry = _registry()
    manifest = build_shard_manifest(registry, shard_counts_by_source={"greenhouse": 2})
    artifacts = [
        _artifact(registry, manifest, "greenhouse-000", stale_board="a"),
        _artifact(registry, manifest, "greenhouse-001", stale_board="a"),
    ]
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")

    strict = persist_shard_artifacts(
        repository=repository,
        registry=registry,
        manifest=manifest,
        artifacts=artifacts,
        payload_retention_hours=72,
        now=lambda: PERSISTED,
    )
    wider = persist_shard_artifacts(
        repository=repository,
        registry=registry,
        manifest=manifest,
        artifacts=artifacts,
        payload_retention_hours=168,
        now=lambda: PERSISTED,
    )

    assert strict.run_id != wider.run_id
    assert strict.replayed is False
    assert wider.replayed is False
    assert strict.metrics.inventory_memberships == 1
    assert wider.metrics.inventory_memberships == 2
    assert len(
        InventoryRunStore(repository).active_job_ids(
            wider.run_id,
            retention_hours=168,
            evaluated_at=PERSISTED,
        )
    ) == 2


def test_completed_replay_reports_persisted_memberships_after_cutoff_moves(
    tmp_path: Path,
) -> None:
    registry = _registry()
    manifest = build_shard_manifest(registry, shard_counts_by_source={"greenhouse": 2})
    artifacts = [
        _artifact(registry, manifest, "greenhouse-000", aging_board="a"),
        _artifact(registry, manifest, "greenhouse-001", aging_board="a"),
    ]
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")

    first = persist_shard_artifacts(
        repository=repository,
        registry=registry,
        manifest=manifest,
        artifacts=artifacts,
        payload_retention_hours=72,
        now=lambda: PERSISTED,
    )
    replay = persist_shard_artifacts(
        repository=repository,
        registry=registry,
        manifest=manifest,
        artifacts=artifacts,
        payload_retention_hours=72,
        now=lambda: PERSISTED + timedelta(hours=2),
    )

    assert first.metrics.inventory_memberships == 2
    assert replay.run_id == first.run_id
    assert replay.replayed is True
    assert replay.metrics.inventory_memberships == 2
    assert InventoryRunStore(repository).membership_count(first.run_id) == 2


def test_fan_in_rejects_invalid_payload_retention_before_mutating_inventory(
    tmp_path: Path,
) -> None:
    registry = _registry()
    manifest = build_shard_manifest(registry, shard_counts_by_source={"greenhouse": 1})
    artifact = _artifact(registry, manifest, "greenhouse-000")
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")

    with pytest.raises(ValueError, match="payload_retention_hours must be at least 1"):
        persist_shard_artifacts(
            repository=repository,
            registry=registry,
            manifest=manifest,
            artifacts=[artifact],
            payload_retention_hours=0,
            now=lambda: PERSISTED,
        )

    assert _inventory_table_exists(repository) is False


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
        _artifact(registry, manifest, "greenhouse-000", stale_board="a"),
        _artifact(registry, manifest, "greenhouse-001", stale_board="a"),
    ]
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")
    original_upsert = repository._upsert_job_in_connection

    def fail_once(connection, job, now):
        raise RuntimeError("simulated persistence failure")

    with monkeypatch.context() as patch:
        patch.setattr(repository, "_upsert_job_in_connection", fail_once)
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
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM inventory_run_jobs").fetchone()[0]
            == 0
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM job_identity_ledger"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM job_retention_evidence"
        ).fetchone()[0] == 0
    assert row is not None
    assert row["status"] == "running"

    monkeypatch.setattr(repository, "_upsert_job_in_connection", original_upsert)
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


def test_fan_in_accepts_provider_company_name_different_from_registry_hint(
    tmp_path: Path,
) -> None:
    registry = _registry()
    manifest = build_shard_manifest(registry, shard_counts_by_source={"greenhouse": 1})
    artifact = _artifact(
        registry,
        manifest,
        "greenhouse-000",
        provider_company="Alpha Holdings, Inc.",
    )
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")

    report = persist_shard_artifacts(
        repository=repository,
        registry=registry,
        manifest=manifest,
        artifacts=[artifact],
        now=lambda: PERSISTED,
    )

    assert report.status == "success"
    with repository.connect() as connection:
        companies = {
            Job.model_validate_json(row["payload_json"]).company
            for row in connection.execute("SELECT payload_json FROM jobs")
        }
    assert "Alpha Holdings, Inc." in companies


def test_fan_in_atomically_closes_skipped_identity_and_reopens_rehydrated_identity(
    tmp_path: Path,
) -> None:
    registry = _registry()
    manifest = build_shard_manifest(registry, shard_counts_by_source={"greenhouse": 2})
    artifacts = [
        _artifact(registry, manifest, "greenhouse-000"),
        _artifact(registry, manifest, "greenhouse-001"),
    ]
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")

    target_a = registry.targets[0].source_target()
    rehydrated = _job(target_a)
    previous = rehydrated.model_copy(
        update={
            "title": "Old Matching Title",
            "content_fingerprint": "old-fingerprint",
        }
    )
    skipped = rehydrated.model_copy(
        update={
            "id": "retained-skipped",
            "source_job_id": "retained-skipped",
            "content_fingerprint": "retained-skipped-fp",
        }
    )
    repository.upsert_job(previous)
    repository.upsert_job(skipped)

    report = persist_shard_artifacts(
        repository=repository,
        registry=registry,
        manifest=manifest,
        artifacts=artifacts,
        invalidated_identities=[
            ("greenhouse", target_a.board_id, previous.source_job_id),
            ("greenhouse", target_a.board_id, skipped.source_job_id),
        ],
        now=lambda: PERSISTED,
    )

    assert report.replayed is False
    with repository.connect() as connection:
        rows = {
            row["source_job_id"]: row["lifecycle"]
            for row in connection.execute(
                "SELECT source_job_id,lifecycle FROM jobs "
                "WHERE source=? AND source_board_id=?",
                ("greenhouse", target_a.board_id),
            ).fetchall()
        }
    assert rows[previous.source_job_id] != "closed"
    assert rows[skipped.source_job_id] == "closed"


def test_fan_in_invalidation_rolls_back_if_atomic_persistence_fails(
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
    target_a = registry.targets[0].source_target()
    retained = _job(target_a).model_copy(
        update={
            "id": "retained-before-failure",
            "source_job_id": "retained-before-failure",
            "content_fingerprint": "retained-before-failure-fp",
        }
    )
    repository.upsert_job(retained)

    def fail_upsert(*_args, **_kwargs):
        raise RuntimeError("simulated persistence failure")

    monkeypatch.setattr(repository, "_upsert_job_in_connection", fail_upsert)

    with pytest.raises(RuntimeError, match="simulated persistence failure"):
        persist_shard_artifacts(
            repository=repository,
            registry=registry,
            manifest=manifest,
            artifacts=artifacts,
            invalidated_identities=[
                ("greenhouse", target_a.board_id, retained.source_job_id)
            ],
            now=lambda: PERSISTED,
        )

    with repository.connect() as connection:
        lifecycle = connection.execute(
            "SELECT lifecycle FROM jobs "
            "WHERE source=? AND source_board_id=? AND source_job_id=?",
            ("greenhouse", target_a.board_id, retained.source_job_id),
        ).fetchone()["lifecycle"]
    assert lifecycle != "closed"


def test_fan_in_run_identity_includes_invalidation_snapshot(tmp_path: Path) -> None:
    registry = _registry()
    manifest = build_shard_manifest(registry, shard_counts_by_source={"greenhouse": 2})
    artifacts = [
        _artifact(registry, manifest, "greenhouse-000"),
        _artifact(registry, manifest, "greenhouse-001"),
    ]

    first = persist_shard_artifacts(
        repository=SQLiteRepository(tmp_path / "first.sqlite3"),
        registry=registry,
        manifest=manifest,
        artifacts=artifacts,
        now=lambda: PERSISTED,
    )
    second = persist_shard_artifacts(
        repository=SQLiteRepository(tmp_path / "second.sqlite3"),
        registry=registry,
        manifest=manifest,
        artifacts=artifacts,
        invalidated_identities=[("greenhouse", "a", "retained")],
        now=lambda: PERSISTED,
    )

    assert first.run_id != second.run_id


def test_fan_in_refuses_invalidation_when_target_collection_is_partial(
    tmp_path: Path,
) -> None:
    registry = _registry()
    manifest = build_shard_manifest(registry, shard_counts_by_source={"greenhouse": 2})
    artifacts = [
        _artifact(registry, manifest, "greenhouse-000", partial_board="a"),
        _artifact(registry, manifest, "greenhouse-001"),
    ]
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")
    target_a = registry.targets[0].source_target()
    retained = _job(target_a).model_copy(
        update={
            "id": "retained-partial",
            "source_job_id": "retained-partial",
            "content_fingerprint": "retained-partial-fp",
        }
    )
    repository.upsert_job(retained)

    with pytest.raises(
        ValueError,
        match="requires successful authoritative target collection",
    ):
        persist_shard_artifacts(
            repository=repository,
            registry=registry,
            manifest=manifest,
            artifacts=artifacts,
            invalidated_identities=[
                ("greenhouse", target_a.board_id, retained.source_job_id)
            ],
            now=lambda: PERSISTED,
        )

    with repository.connect() as connection:
        row = connection.execute(
            "SELECT lifecycle,payload_json FROM jobs "
            "WHERE source=? AND source_board_id=? AND source_job_id=?",
            ("greenhouse", target_a.board_id, retained.source_job_id),
        ).fetchone()
        run_count = connection.execute(
            "SELECT COUNT(*) FROM inventory_runs"
        ).fetchone()[0]

    assert row is not None
    assert row["lifecycle"] != "closed"
    assert Job.model_validate_json(row["payload_json"]).description_text is not None
    assert run_count == 0


def test_fan_in_compacts_invalidated_payload_and_preserves_identity_ledger(
    tmp_path: Path,
) -> None:
    registry = _registry()
    manifest = build_shard_manifest(registry, shard_counts_by_source={"greenhouse": 2})
    artifacts = [
        _artifact(registry, manifest, "greenhouse-000"),
        _artifact(registry, manifest, "greenhouse-001"),
    ]
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")
    target_a = registry.targets[0].source_target()
    retained = _job(target_a).model_copy(
        update={
            "id": "retained-compact",
            "source_job_id": "retained-compact",
            "description_text": "Sensitive full description",
            "description_html": "<p>Sensitive full description</p>",
            "raw_metadata": {"provider": "full-payload"},
            "offices": ["Remote"],
            "content_fingerprint": "retained-compact-fp",
        }
    )
    repository.upsert_job(retained)

    persist_shard_artifacts(
        repository=repository,
        registry=registry,
        manifest=manifest,
        artifacts=artifacts,
        invalidated_identities=[
            ("greenhouse", target_a.board_id, retained.source_job_id)
        ],
        now=lambda: PERSISTED,
    )

    with repository.connect() as connection:
        row = connection.execute(
            "SELECT lifecycle,payload_json FROM jobs "
            "WHERE source=? AND source_board_id=? AND source_job_id=?",
            ("greenhouse", target_a.board_id, retained.source_job_id),
        ).fetchone()
        ledger = connection.execute(
            "SELECT pruned_at FROM job_identity_ledger "
            "WHERE source=? AND source_board_id=? AND source_job_id=?",
            ("greenhouse", target_a.board_id, retained.source_job_id),
        ).fetchone()
        retention = connection.execute(
            "SELECT 1 FROM job_retention_evidence WHERE job_id=?",
            (retained.id,),
        ).fetchone()

    assert row is not None
    assert row["lifecycle"] == "closed"
    compact = Job.model_validate_json(row["payload_json"])
    assert compact.description_text is None
    assert compact.description_html is None
    assert compact.raw_metadata == {}
    assert compact.offices == []
    assert ledger is not None
    assert ledger["pruned_at"] == PERSISTED.isoformat()
    assert retention is None
