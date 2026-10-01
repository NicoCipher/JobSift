from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from job_scout.domain.models import (
    CollectionResult,
    CollectionStatus,
    Job,
    PostingFreshnessRule,
    RuleIntent,
    SearchBrief,
    SourceTarget,
    UnknownEligibilityPolicy,
)
from job_scout.production_registry import (
    ProductionSourceRegistry,
    build_shard_manifest,
)
from job_scout.shard_benchmark import (
    build_benchmark_plan,
    require_local_benchmark_environment,
    run_benchmark_fan_in,
    select_registry_subset,
)
from job_scout.shard_collection import collect_shard
from job_scout.storage.sqlite import SQLiteRepository

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _registry() -> ProductionSourceRegistry:
    targets = [
        {
            "target_identity": "ashby:alpha",
            "source": "ashby",
            "coordinates": {"board": "alpha"},
            "company_hint": "Ash Alpha",
            "health_current_postings": 3,
            "health_inventory_exact": True,
        },
        {
            "target_identity": "ashby:beta",
            "source": "ashby",
            "coordinates": {"board": "beta"},
            "company_hint": "Ash Beta",
            "health_current_postings": 2,
            "health_inventory_exact": True,
        },
        {
            "target_identity": "greenhouse:alpha",
            "source": "greenhouse",
            "coordinates": {"board": "alpha"},
            "company_hint": "Green Alpha",
            "health_current_postings": 4,
            "health_inventory_exact": True,
        },
        {
            "target_identity": "greenhouse:beta",
            "source": "greenhouse",
            "coordinates": {"board": "beta"},
            "company_hint": "Green Beta",
            "health_current_postings": 1,
            "health_inventory_exact": True,
        },
        {
            "target_identity": "lever:global:alpha",
            "source": "lever",
            "coordinates": {"instance": "global", "site": "alpha"},
            "company_hint": "Lever Alpha",
            "health_current_postings": 2,
            "health_inventory_exact": True,
        },
        {
            "target_identity": "lever:global:beta",
            "source": "lever",
            "coordinates": {"instance": "global", "site": "beta"},
            "company_hint": "Lever Beta",
            "health_current_postings": 2,
            "health_inventory_exact": True,
        },
        {
            "target_identity": "workday:alpha.wd1.myworkdayjobs.com:alpha:External",
            "source": "workday",
            "coordinates": {
                "host": "alpha.wd1.myworkdayjobs.com",
                "tenant": "alpha",
                "site": "External",
            },
            "company_hint": "Work Alpha",
            "health_current_postings": 2,
            "health_inventory_exact": True,
        },
        {
            "target_identity": "workday:beta.wd1.myworkdayjobs.com:beta:External",
            "source": "workday",
            "coordinates": {
                "host": "beta.wd1.myworkdayjobs.com",
                "tenant": "beta",
                "site": "External",
            },
            "company_hint": "Work Beta",
            "health_current_postings": 2,
            "health_inventory_exact": True,
        },
    ]
    return ProductionSourceRegistry(
        registry_id="test-production",
        target_universe_git_blob_sha="b" * 40,
        health_manifest_sha256="a" * 64,
        health_evidence_updated_at="2026-09-09T05:13:04+00:00",
        target_counts_by_source={
            "ashby": 2,
            "greenhouse": 2,
            "lever": 2,
            "workday": 2,
        },
        targets=targets,
    )


def _all(value: int) -> dict[str, int]:
    return {
        "greenhouse": value,
        "ashby": value,
        "workday": value,
        "lever": value,
    }


def test_bounded_subset_is_deterministic_and_retains_production_provenance() -> None:
    registry = _registry()

    first = select_registry_subset(registry, target_limits_by_source=_all(1))
    second = select_registry_subset(registry, target_limits_by_source=_all(1))

    assert first == second
    assert len(first.targets) == 4
    assert first.target_counts_by_source == {
        "ashby": 1,
        "greenhouse": 1,
        "lever": 1,
        "workday": 1,
    }
    assert first.target_universe_git_blob_sha == registry.target_universe_git_blob_sha
    assert first.health_manifest_sha256 == registry.health_manifest_sha256
    assert first.registry_id.startswith("test-production-bounded-")


def test_benchmark_plan_builds_provider_isolated_matrix() -> None:
    registry = _registry()
    plan, subset, manifest = build_benchmark_plan(
        registry,
        target_limits_by_source=_all(2),
        shard_counts_by_source=_all(1),
    )

    assert plan.total_targets == 8
    assert plan.total_shards == 4
    assert plan.workday_detail_concurrency == 1
    assert len(plan.matrix["include"]) == 4
    assert {row["source"] for row in plan.matrix["include"]} == set(_all(1))
    assert manifest.registry_id == subset.registry_id
    assert {shard.source for shard in manifest.shards} == set(_all(1))


def test_benchmark_plan_rejects_shards_above_selected_targets() -> None:
    with pytest.raises(ValueError, match="shard count exceeds selected"):
        build_benchmark_plan(
            _registry(),
            target_limits_by_source=_all(1),
            shard_counts_by_source={
                "greenhouse": 2,
                "ashby": 1,
                "workday": 1,
                "lever": 1,
            },
        )


def _job(
    source: str,
    target: SourceTarget,
    *,
    posted_at: datetime | None = NOW - timedelta(hours=2),
) -> Job:
    source_id = f"{source}-{target.board_id}"
    return Job(
        id=source_id,
        source=source,
        source_job_id=source_id,
        source_board_id=target.board_id,
        title="Software Engineer",
        company=target.company,
        employer_id=target.employer_id,
        description_text="Build and maintain reliable software products. " * 10,
        job_url=f"https://example.test/{source}/{source_id}",
        canonical_url=f"https://example.test/{source}/{source_id}",
        country="United States",
        eligible_countries={"United States"},
        posted_at=posted_at,
        discovered_at=NOW,
        last_seen_at=NOW,
        content_fingerprint=f"fp-{source_id}",
    )


def test_benchmark_fan_in_evaluates_shared_inventory_without_delivery(tmp_path: Path) -> None:
    registry = select_registry_subset(_registry(), target_limits_by_source=_all(1))
    manifest = build_shard_manifest(registry, shard_counts_by_source=_all(1))

    class Collector:
        client = None

        def __init__(self, source: str) -> None:
            self.source = source

        def collect(self, target: SourceTarget) -> CollectionResult:
            return CollectionResult(
                source=self.source,
                target=target,
                status=CollectionStatus.SUCCESS,
                jobs=[_job(self.source, target)],
                raw_postings_received=1,
            )

    artifacts = [
        collect_shard(
            registry=registry,
            manifest=manifest,
            shard_id=shard.shard_id,
            collector_factory=Collector,
            now=lambda: NOW,
            monotonic=lambda: 0.0,
        )
        for shard in manifest.shards
    ]
    brief = SearchBrief(
        client_id="benchmark-client",
        target_roles=["Software Engineer"],
        management_roles=RuleIntent.IGNORE,
    )
    brief_path = tmp_path / "brief.json"
    brief_path.write_text(brief.model_dump_json(), encoding="utf-8")
    repository = SQLiteRepository(tmp_path / "benchmark.sqlite3")

    report = run_benchmark_fan_in(
        repository=repository,
        registry=registry,
        manifest=manifest,
        artifacts=artifacts,
        brief_path=brief_path,
    )

    assert report.fan_in.status == "success"
    assert report.fan_in.metrics.targets_attempted == 4
    assert report.active_inventory_jobs == 4
    assert report.evaluation is not None
    assert report.evaluation.total_evaluated == 4
    assert report.evaluation.semantic_matches_before_freshness == 4
    assert report.evaluation.freshness_eligible_matches == 4
    assert report.distinct_matched_employers == 4
    assert report.distinct_matched_delivery_groups == 4

    with repository.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM exports").fetchone()[0] == 0


def test_benchmark_rejects_turso_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://production.example")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "secret")

    with pytest.raises(ValueError, match="must not use Turso"):
        require_local_benchmark_environment()


def test_benchmark_match_yield_applies_production_freshness_policy(tmp_path: Path) -> None:
    registry = select_registry_subset(_registry(), target_limits_by_source=_all(1))
    manifest = build_shard_manifest(registry, shard_counts_by_source=_all(1))

    class Collector:
        client = None

        def __init__(self, source: str) -> None:
            self.source = source

        def collect(self, target: SourceTarget) -> CollectionResult:
            posted_at = (
                NOW - timedelta(hours=30)
                if self.source == "ashby"
                else None
                if self.source == "workday"
                else NOW - timedelta(hours=2)
            )
            return CollectionResult(
                source=self.source,
                target=target,
                status=CollectionStatus.SUCCESS,
                jobs=[_job(self.source, target, posted_at=posted_at)],
                raw_postings_received=1,
            )

    artifacts = [
        collect_shard(
            registry=registry,
            manifest=manifest,
            shard_id=shard.shard_id,
            collector_factory=Collector,
            now=lambda: NOW,
            monotonic=lambda: 0.0,
        )
        for shard in manifest.shards
    ]
    brief = SearchBrief(
        client_id="benchmark-freshness-client",
        target_roles=["Software Engineer"],
        management_roles=RuleIntent.IGNORE,
        posting_freshness=PostingFreshnessRule(
            max_age_hours=24,
            unknown_policy=UnknownEligibilityPolicy.REJECT,
        ),
    )
    brief_path = tmp_path / "freshness-brief.json"
    brief_path.write_text(brief.model_dump_json(), encoding="utf-8")

    report = run_benchmark_fan_in(
        repository=SQLiteRepository(tmp_path / "freshness.sqlite3"),
        registry=registry,
        manifest=manifest,
        artifacts=artifacts,
        brief_path=brief_path,
    )

    assert report.evaluation is not None
    assert report.evaluation.semantic_matches_before_freshness == 4
    assert report.evaluation.freshness_eligible_matches == 2
    assert report.evaluation.stale_posting_suppressions == 1
    assert report.evaluation.unknown_age_suppressions == 1
    assert report.evaluation.invalid_time_suppressions == 0
    assert report.distinct_matched_employers == 2
    assert report.distinct_matched_delivery_groups == 2


def test_benchmark_plan_records_bounded_workday_detail_concurrency() -> None:
    plan, _, _ = build_benchmark_plan(
        _registry(),
        target_limits_by_source=_all(1),
        shard_counts_by_source=_all(1),
        workday_detail_concurrency=4,
    )

    assert plan.workday_detail_concurrency == 4


@pytest.mark.parametrize("value", [0, 9])
def test_benchmark_plan_rejects_unsafe_workday_detail_concurrency(value: int) -> None:
    with pytest.raises(ValueError, match="Workday detail concurrency"):
        build_benchmark_plan(
            _registry(),
            target_limits_by_source=_all(1),
            shard_counts_by_source=_all(1),
            workday_detail_concurrency=value,
        )
