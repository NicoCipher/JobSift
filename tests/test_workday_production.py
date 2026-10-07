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
            external_path=f"/job/example/Sales-Associate_R{i}",
            title="Sales Associate",
            posted_on="Posted Today",
        )
        for i in range(12_524)
    ]
    candidate_indexes = (7, 4_321, 12_000)
    for index in candidate_indexes:
        postings[index] = WorkdayIndexPosting(
            external_path=f"/job/example/Software-Engineer_R{index}",
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
        f"/job/example/Software-Engineer_R{index}" for index in candidate_indexes
    ]
    assert collector.last_counts["candidate_paths"] == 3
    assert collector.last_counts["index_title_skipped"] == 12_521
    assert collector.last_counts["detail_attempts"] == 3
    assert collector.last_counts["fallback_full_collection"] is False


def test_unknown_index_evidence_is_hydrated_not_silently_rejected():
    registry, production_target = _target()
    postings = [
        WorkdayIndexPosting(
            external_path="/job/example/Software-Engineer_M1",
            title="Software Engineer",
            posted_on=None,
        ),
        WorkdayIndexPosting(
            external_path="/job/example/Unknown_M2",
            title=None,
            posted_on="Posted Today",
        ),
        WorkdayIndexPosting(
            external_path="/job/example/Software-Engineer_M3",
            title="Software Engineer",
            posted_on="Posted 4 Days Ago",
        ),
        WorkdayIndexPosting(
            external_path="/job/example/Sales-Associate_M4",
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
        "/job/example/Software-Engineer_M1",
        "/job/example/Unknown_M2",
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
                external_path="/job/example/Software-Engineer_V1",
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
                    destination_id="jobs",
                    sourcing_plan_id="active-plan",
                ),
            )

    monkeypatch.setattr(workday_production, "ClientDeliveryProfileStore", FakeProfileStore)
    monkeypatch.setattr(
        workday_production,
        "OperatorClientStore",
        lambda _repository: SimpleNamespace(
            get_for_profile=lambda _client_id, _destination_id: None
        ),
    )

    bindings = workday_production.resolve_active_workday_brief_bindings(
        repository=object(),
        plan_dir=plan_dir,
        repo_root=tmp_path,
    )

    assert len(bindings) == 1
    assert bindings[0].path == "config/search_briefs/active.json"
    workday_production.verify_active_workday_brief_bindings(
        repository=object(),
        plan_dir=plan_dir,
        repo_root=tmp_path,
        expected=bindings,
    )

    class ChangedProfileStore:
        def __init__(self, _repository) -> None:
            pass

        def active(self):
            return (
                SimpleNamespace(
                    client_id="example",
                    destination_id="jobs",
                    sourcing_plan_id="example-plan",
                ),
            )

    monkeypatch.setattr(
        workday_production,
        "ClientDeliveryProfileStore",
        ChangedProfileStore,
    )
    with pytest.raises(ValueError, match="snapshot changed"):
        workday_production.verify_active_workday_brief_bindings(
            repository=object(),
            plan_dir=plan_dir,
            repo_root=tmp_path,
            expected=bindings,
        )

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


def test_nonmatching_and_stale_rows_remain_pruned_without_list_identity_guessing():
    registry, production_target = _target()
    postings = [
        WorkdayIndexPosting(
            external_path="/job/example/Sales-Associate_LIST-ALIAS",
            title="Sales Associate",
            posted_on="Posted Today",
        ),
        WorkdayIndexPosting(
            external_path="/job/example/Software-Engineer_OTHER-ALIAS",
            title="Software Engineer",
            posted_on="Posted 4 Days Ago",
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

    result = collector.collect(production_target.source_target())

    assert result.status is CollectionStatus.SUCCESS
    assert hydrator.paths is None
    assert collector.last_counts["index_title_skipped"] == 1
    assert collector.last_counts["index_stale_skipped"] == 1


def test_retained_candidate_snapshot_keeps_only_titles_matching_active_briefs(tmp_path):
    from job_scout.domain.models import Job
    from job_scout.storage.sqlite import SQLiteRepository

    _registry, production_target = _target()
    board_id = production_target.source_target().board_id
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")

    for source_job_id, title in (
        ("R-match", "Software Engineer"),
        ("R-skip", "Sales Associate"),
    ):
        repository.upsert_job(
            Job(
                id=f"job-{source_job_id}",
                source="workday",
                source_job_id=source_job_id,
                source_board_id=board_id,
                title=title,
                company="Example",
                job_url=f"https://example.test/jobs/{source_job_id}",
                canonical_url=f"https://example.test/jobs/{source_job_id}",
                posted_at=NOW,
                content_fingerprint=f"fingerprint-{source_job_id}",
            )
        )

    bindings = workday_production.resolve_retained_workday_candidate_bindings(
        repository=repository,
        briefs=[SearchBrief(client_id="client-a", target_roles=["Software Engineer"])],
    )

    assert bindings == [
        workday_production.WorkdayRetainedCandidateBinding(
            board_id=board_id,
            source_job_ids=["R-match"],
        )
    ]


def test_active_profile_binding_supports_durable_operator_brief(tmp_path):
    from job_scout.delivery_profiles import ClientDeliveryProfileStore
    from job_scout.operator_clients import OperatorClientStore
    from job_scout.storage.sqlite import SQLiteRepository

    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")
    brief = SearchBrief(
        client_id="client-a",
        target_roles=["Platform Engineer"],
    )
    OperatorClientStore(repository).upsert(
        client_id="client-a",
        display_name="Acme",
        destination_id="jobs",
        destination_name="Acme Jobs",
        sourcing_plan_id="operator-client-a",
        brief=brief,
    )
    ClientDeliveryProfileStore(repository).upsert(
        client_id="client-a",
        destination_id="jobs",
        sourcing_plan_id="operator-client-a",
        daily_quota=100,
        status="active",
        delivery_mode="review",
        timezone="Africa/Lagos",
    )

    plan_dir = tmp_path / "config" / "sourcing_plans"
    plan_dir.mkdir(parents=True)
    bindings = workday_production.resolve_active_workday_brief_bindings(
        repository=repository,
        plan_dir=plan_dir,
        repo_root=tmp_path,
    )

    assert len(bindings) == 1
    assert bindings[0].path is None
    assert bindings[0].brief_json is not None
    loaded = workday_production.load_bound_workday_briefs(
        bindings,
        repo_root=tmp_path,
    )
    assert loaded == [brief]
