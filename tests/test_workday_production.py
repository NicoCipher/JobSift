from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from job_scout import workday_production
from job_scout.domain.models import CollectionResult, CollectionStatus, SearchBrief
from job_scout.production_registry import load_production_registry
from job_scout.workday_index import WorkdayIndexPosting, WorkdayIndexTargetResult

REGISTRY = Path("config/source_registries/production_active_v1.json")
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def _target():
    registry = load_production_registry(REGISTRY)
    production_target = next(target for target in registry.targets if target.source == "workday")
    return registry, production_target


class FakeScanner:
    def __init__(self, result: WorkdayIndexTargetResult) -> None:
        self.result = result
        self.closed = False

    def scan(self, _target):
        return self.result

    def close(self) -> None:
        self.closed = True


class FakeHydrator:
    def __init__(self, *, full_raw: int = 0) -> None:
        self.paths: list[str] | None = None
        self.full_raw = full_raw
        self.full_called = False
        self.last_counts = {}

    def hydrate_paths(self, target, paths):
        self.paths = list(paths)
        self.last_counts = {
            "coverage_mode": "path_hydration",
            "detail_attempts": len(paths),
            "normalized": 0,
        }
        return CollectionResult(
            source="workday",
            target=target,
            status=CollectionStatus.SUCCESS,
            raw_postings_received=len(paths),
        )

    def collect(self, target):
        self.full_called = True
        self.last_counts = {
            "coverage_mode": "broad",
            "detail_attempts": self.full_raw,
            "normalized": 0,
        }
        return CollectionResult(
            source="workday",
            target=target,
            status=CollectionStatus.SUCCESS,
            raw_postings_received=self.full_raw,
        )


def _index_result(target_identity: str, postings, *, status=CollectionStatus.SUCCESS, errors=None):
    return WorkdayIndexTargetResult(
        target_identity=target_identity,
        status=status,
        started_at=NOW,
        completed_at=NOW,
        runtime_ms=10,
        broad_total=len(postings),
        provider_rows_seen=len(postings),
        coverage_mode="broad" if status is CollectionStatus.SUCCESS else "capped_partial",
        postings=list(postings),
        errors=list(errors or []),
    )


def test_monster_board_hydrates_only_bounded_candidate_union():
    registry, production_target = _target()
    postings = [
        WorkdayIndexPosting(
            external_path=f"/job/example/R{i}",
            title="Sales Associate",
            posted_on="Posted Today",
        )
        for i in range(12_524)
    ]
    candidate_indexes = (7, 4_321, 12_000)
    for index in candidate_indexes:
        postings[index] = WorkdayIndexPosting(
            external_path=f"/job/example/R{index}",
            title="Software Engineer",
            posted_on="Posted Today",
        )

    scanner_result = _index_result(production_target.target_identity, postings)
    hydrator = FakeHydrator()
    collector = workday_production.IndexFirstWorkdayCollector(
        registry=registry,
        briefs=[SearchBrief(client_id="client-a", target_roles=["Software Engineer"])],
        detail_concurrency=6,
        scanner_factory=lambda: FakeScanner(scanner_result),
        hydration_factory=lambda _concurrency: hydrator,
    )

    result = collector.collect(production_target.source_target())

    assert result.status is CollectionStatus.SUCCESS
    assert result.raw_postings_received == 12_524
    assert hydrator.paths == [
        f"/job/example/R{index}" for index in candidate_indexes
    ]
    assert collector.last_counts["candidate_paths"] == 3
    assert collector.last_counts["index_title_skipped"] == 12_521
    assert collector.last_counts["detail_attempts"] == 3
    assert collector.last_counts["fallback_full_collection"] is False


def test_unknown_index_evidence_is_hydrated_not_silently_rejected():
    registry, production_target = _target()
    postings = [
        WorkdayIndexPosting(
            external_path="/job/example/matching",
            title="Software Engineer",
            posted_on=None,
        ),
        WorkdayIndexPosting(
            external_path="/job/example/missing-title",
            title=None,
            posted_on="Posted Today",
        ),
        WorkdayIndexPosting(
            external_path="/job/example/stale",
            title="Software Engineer",
            posted_on="Posted 4 Days Ago",
        ),
        WorkdayIndexPosting(
            external_path="/job/example/unrelated",
            title="Sales Associate",
            posted_on="Posted Today",
        ),
    ]
    hydrator = FakeHydrator()
    collector = workday_production.IndexFirstWorkdayCollector(
        registry=registry,
        briefs=[SearchBrief(client_id="client-a", target_roles=["Software Engineer"])],
        detail_concurrency=4,
        scanner_factory=lambda: FakeScanner(
            _index_result(production_target.target_identity, postings)
        ),
        hydration_factory=lambda _concurrency: hydrator,
    )

    collector.collect(production_target.source_target())

    assert hydrator.paths == [
        "/job/example/matching",
        "/job/example/missing-title",
    ]
    assert collector.last_counts["uncertain_title_hydrated"] == 1
    assert collector.last_counts["index_stale_skipped"] == 1
    assert collector.last_counts["index_title_skipped"] == 1


def test_incomplete_index_falls_back_to_legacy_full_collection():
    registry, production_target = _target()
    partial = _index_result(
        production_target.target_identity,
        [
            WorkdayIndexPosting(
                external_path="/job/example/visible",
                title="Software Engineer",
                posted_on="Posted Today",
            )
        ],
        status=CollectionStatus.PARTIAL,
        errors=["broad offset 20: HTTP 503"],
    )
    hydrator = FakeHydrator(full_raw=12_524)
    collector = workday_production.IndexFirstWorkdayCollector(
        registry=registry,
        briefs=[SearchBrief(client_id="client-a", target_roles=["Software Engineer"])],
        detail_concurrency=4,
        scanner_factory=lambda: FakeScanner(partial),
        hydration_factory=lambda _concurrency: hydrator,
    )

    result = collector.collect(production_target.source_target())

    assert result.status is CollectionStatus.SUCCESS
    assert result.raw_postings_received == 12_524
    assert hydrator.full_called is True
    assert hydrator.paths is None
    assert collector.last_counts["fallback_full_collection"] is True
    assert collector.last_counts["index_errors"] == 1


def test_active_profile_bindings_ignore_unbound_example_plans(tmp_path, monkeypatch):
    plan_dir = tmp_path / "config" / "sourcing_plans"
    brief_dir = tmp_path / "config" / "search_briefs"
    plan_dir.mkdir(parents=True)
    brief_dir.mkdir(parents=True)

    active_brief = SearchBrief(client_id="client-a", target_roles=["Software Engineer"])
    example_brief = SearchBrief(client_id="example", target_roles=["Example Role"])
    (brief_dir / "active.json").write_text(active_brief.model_dump_json(), encoding="utf-8")
    (brief_dir / "example.json").write_text(example_brief.model_dump_json(), encoding="utf-8")

    active_plan = {
        "plan_version": "sourcing-plan-v1",
        "plan_id": "active-plan",
        "search_brief": "../search_briefs/active.json",
        "database": "../../runtime/jobs.sqlite3",
        "csv": "../../exports/jobs.csv",
        "targets": [
            {
                "source": "greenhouse",
                "company": "Example",
                "board": "example",
            }
        ],
    }
    example_plan = {
        **active_plan,
        "plan_id": "example-plan",
        "search_brief": "../search_briefs/example.json",
    }
    (plan_dir / "active.json").write_text(json.dumps(active_plan), encoding="utf-8")
    (plan_dir / "example.json").write_text(json.dumps(example_plan), encoding="utf-8")

    class FakeProfileStore:
        def __init__(self, _repository) -> None:
            pass

        def active(self):
            return (
                SimpleNamespace(
                    client_id="client-a",
                    sourcing_plan_id="active-plan",
                ),
            )

    monkeypatch.setattr(workday_production, "ClientDeliveryProfileStore", FakeProfileStore)

    bindings = workday_production.resolve_active_workday_brief_bindings(
        repository=object(),
        plan_dir=plan_dir,
        repo_root=tmp_path,
    )

    assert len(bindings) == 1
    assert bindings[0].path == "config/search_briefs/active.json"
    loaded = workday_production.load_bound_workday_briefs(
        bindings,
        repo_root=tmp_path,
    )
    assert [brief.client_id for brief in loaded] == ["client-a"]

    (brief_dir / "active.json").write_text(
        SearchBrief(client_id="client-a", target_roles=["Changed Role"]).model_dump_json(),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="changed after planning"):
        workday_production.load_bound_workday_briefs(bindings, repo_root=tmp_path)
