from datetime import UTC, datetime, timedelta
from pathlib import Path

from job_scout import inventory_refresh
from job_scout.storage.inventory_runs import InventoryRunStore
from job_scout.storage.sqlite import SQLiteRepository

REGISTRY = Path("config/source_registries/production_active_v1.json")


def test_rotating_refresh_plan_is_bounded_and_changes_cohort():
    registry = inventory_refresh.load_production_registry(REGISTRY)

    plan0, subset0, manifest0 = inventory_refresh.build_refresh_plan(
        registry=registry, cohort=0
    )
    _plan1, subset1, manifest1 = inventory_refresh.build_refresh_plan(
        registry=registry, cohort=1
    )

    limits = inventory_refresh.default_refresh_limits(registry)
    assert sum(limits.values()) == 100
    assert len(subset0.targets) == 100
    assert subset0.target_counts_by_source == limits
    assert plan0.total_targets == 100
    assert len(manifest0.shards) == plan0.total_shards
    assert [target.target_identity for target in subset0.targets] == sorted(
        target.target_identity for target in subset0.targets
    )

    first = {target.target_identity for target in subset0.targets}
    second = {target.target_identity for target in subset1.targets}
    assert first != second
    assert subset1.target_counts_by_source == limits
    assert manifest1.registry_id == subset1.registry_id


def test_smartrecruiters_budget_does_not_make_small_greenhouse_limit_nonpositive():
    from job_scout.production_registry import ProductionSourceRegistry, ProductionTarget

    targets = [
        *[
            ProductionTarget(
                source="greenhouse",
                target_identity=f"greenhouse:green-{index}",
                coordinates={"board": f"green-{index}"},
                company_hint=f"Green {index}",
            )
            for index in range(5)
        ],
        *[
            ProductionTarget(
                source="smartrecruiters",
                target_identity=f"smartrecruiters:smart-{index}",
                coordinates={"board": f"smart-{index}"},
                company_hint=f"Smart {index}",
            )
            for index in range(7)
        ],
    ]
    registry = ProductionSourceRegistry(
        registry_id="small-greenhouse-smartrecruiters",
        targets=sorted(targets, key=lambda target: target.target_identity),
        target_counts_by_source={"greenhouse": 5, "smartrecruiters": 7},
        health_evidence_sha256="a" * 64,
    )

    limits = inventory_refresh.default_refresh_limits(registry)

    assert limits == {"greenhouse": 5, "smartrecruiters": 7}
    plan, subset, _manifest = inventory_refresh.build_refresh_plan(
        registry=registry,
        cohort=0,
        limits=limits,
    )
    assert plan.total_targets == 12
    assert subset.target_counts_by_source == limits


def test_dynamic_smartrecruiters_rotation_stays_inside_24_hour_window():
    from job_scout.production_registry import ProductionTarget

    registry = inventory_refresh.load_production_registry(REGISTRY)
    payload = registry.model_dump(mode="json")
    payload["targets"].extend(
        ProductionTarget(
            source="smartrecruiters",
            target_identity=f"smartrecruiters:dynamic-{index}",
            coordinates={"board": f"dynamic-{index}"},
            company_hint=f"Dynamic {index}",
        ).model_dump(mode="json")
        for index in range(164)
    )
    payload["targets"].sort(key=lambda target: target["target_identity"])
    payload["target_counts_by_source"]["smartrecruiters"] = 164
    expanded = type(registry).model_validate(payload)

    limits = inventory_refresh.default_refresh_limits(expanded, workday_limit=25)
    assert limits["greenhouse"] == 33
    assert limits["smartrecruiters"] == 7
    assert limits["workday"] == 25
    assert sum(limits.values()) == 124

    proof = inventory_refresh.build_coverage_proof(expanded, limits=limits)
    smartrecruiters = next(
        item for item in proof.providers if item.source == "smartrecruiters"
    )
    assert smartrecruiters.targets_per_cohort == 7
    assert smartrecruiters.worst_case_full_coverage_cohorts <= 24

def test_default_rotation_proves_full_registry_coverage():
    registry = inventory_refresh.load_production_registry(REGISTRY)
    limits = inventory_refresh.default_refresh_limits(registry)

    proof = inventory_refresh.build_coverage_proof(registry, limits=limits)

    assert proof.all_targets_reached is True
    assert {item.source for item in proof.providers} == set(limits)
    for item in proof.providers:
        assert item.all_targets_reached is True
        assert item.deterministic_wraparound is True
        assert item.worst_case_full_coverage_cohorts <= item.max_revisit_gap_cohorts
        assert item.visits_per_target_min >= 1
        assert item.visits_per_target_max >= item.visits_per_target_min

    workday = next(item for item in proof.providers if item.source == "workday")
    assert workday.targets_per_cohort == 1
    assert workday.selection_period_cohorts == registry.target_counts_by_source["workday"]
    assert workday.max_revisit_gap_cohorts == registry.target_counts_by_source["workday"]
    assert workday.worst_case_full_coverage_cohorts == registry.target_counts_by_source["workday"]


def test_coverage_math_handles_non_coprime_rotation():
    proof = inventory_refresh._provider_coverage_proof(
        source="example",
        registry_targets=12,
        targets_per_cohort=4,
    )

    assert proof.selection_period_cohorts == 3
    assert proof.visits_per_target_min == 1
    assert proof.visits_per_target_max == 1
    assert proof.max_revisit_gap_cohorts == 3
    assert proof.worst_case_full_coverage_cohorts == 3
    assert proof.all_targets_reached is True
    assert proof.deterministic_wraparound is True


def test_observed_coverage_report_tracks_revisits_and_registry_scope(tmp_path):
    registry = inventory_refresh.load_production_registry(REGISTRY)
    limits = inventory_refresh.default_refresh_limits(registry)
    _plan, subset, _manifest = inventory_refresh.build_refresh_plan(
        registry=registry,
        cohort=0,
        limits=limits,
    )
    repo = SQLiteRepository(tmp_path / "coverage.sqlite3")
    store = InventoryRunStore(repo)
    first_at = datetime(2026, 10, 3, 0, 0, tzinfo=UTC)

    def persist(run_id: str, observed_at: datetime) -> None:
        store.create(run_id=run_id, plan_id=subset.registry_id, started_at=observed_at)
        store.persist_jobs_and_finish(
            run_id=run_id,
            jobs=[],
            memberships=[],
            target_observations=[
                (
                    target.target_identity,
                    target.source,
                    "success",
                    observed_at,
                    observed_at,
                    100,
                    3,
                    2,
                    1,
                    1,
                )
                for target in subset.targets
            ],
            status="success",
            completed_at=observed_at,
        )

    persist("run-1", first_at)
    first = inventory_refresh.build_observed_coverage_report(
        repository=repo,
        run_id="run-1",
        parent_registry=registry,
        selected_registry=subset,
        generated_at=first_at,
    )

    assert len(first.targets) == len(subset.targets)
    assert all(target.revisit_hours is None for target in first.targets)
    for provider in first.providers:
        assert provider.current_targets_attempted == limits[provider.source]
        assert provider.approved_targets_observed_ever == limits[provider.source]
        assert provider.approved_targets_never_observed == (
            registry.target_counts_by_source[provider.source] - limits[provider.source]
        )
        assert provider.revisit_samples == 0

    second_at = first_at + timedelta(hours=2)
    persist("run-2", second_at)
    second = inventory_refresh.build_observed_coverage_report(
        repository=repo,
        run_id="run-2",
        parent_registry=registry,
        selected_registry=subset,
        generated_at=second_at,
    )

    assert all(target.revisit_hours == 2.0 for target in second.targets)
    for provider in second.providers:
        assert provider.revisit_samples == limits[provider.source]
        assert provider.max_observed_revisit_hours == 2.0
        assert provider.p95_observed_revisit_hours == 2.0
        assert provider.timestamp_known_rate == 0.5


def test_observed_coverage_replay_ignores_future_target_state(tmp_path):
    registry = inventory_refresh.load_production_registry(REGISTRY)
    limits = inventory_refresh.default_refresh_limits(registry)
    _plan0, subset0, _manifest0 = inventory_refresh.build_refresh_plan(
        registry=registry,
        cohort=0,
        limits=limits,
    )
    _plan1, subset1, _manifest1 = inventory_refresh.build_refresh_plan(
        registry=registry,
        cohort=1,
        limits=limits,
    )
    repo = SQLiteRepository(tmp_path / "coverage-replay.sqlite3")
    store = InventoryRunStore(repo)
    first_at = datetime(2026, 10, 3, 0, 0, tzinfo=UTC)
    second_at = first_at + timedelta(hours=2)

    def persist(run_id: str, observed_at: datetime, subset) -> None:
        store.create(run_id=run_id, plan_id=subset.registry_id, started_at=observed_at)
        store.persist_jobs_and_finish(
            run_id=run_id,
            jobs=[],
            memberships=[],
            target_observations=[
                (
                    target.target_identity,
                    target.source,
                    "success",
                    observed_at,
                    observed_at,
                    100,
                    3,
                    2,
                    1,
                    1,
                )
                for target in subset.targets
            ],
            status="success",
            completed_at=observed_at,
        )

    persist("run-old", first_at, subset0)
    persist("run-new", second_at, subset1)

    replay = inventory_refresh.build_observed_coverage_report(
        repository=repo,
        run_id="run-old",
        parent_registry=registry,
        selected_registry=subset0,
        generated_at=first_at,
    )

    for provider in replay.providers:
        assert provider.max_hours_since_last_observation == 0.0
        assert provider.approved_targets_observed_ever == limits[provider.source]
        assert provider.approved_targets_never_observed == (
            registry.target_counts_by_source[provider.source] - limits[provider.source]
        )


def test_refresh_includes_approved_smartrecruiters_targets():
    from job_scout.production_registry import ProductionTarget

    registry = inventory_refresh.load_production_registry(REGISTRY)
    payload = registry.model_dump(mode="json")
    payload["targets"].append(ProductionTarget(
        source="smartrecruiters", target_identity="smartrecruiters:acme",
        coordinates={"board": "acme"}, company_hint="Acme",
    ).model_dump(mode="json"))
    payload["targets"].sort(key=lambda target: target["target_identity"])
    payload["target_counts_by_source"]["smartrecruiters"] = 1
    registry = type(registry).model_validate(payload)
    plan, subset, manifest = inventory_refresh.build_refresh_plan(registry=registry, cohort=0)
    assert subset.target_counts_by_source["smartrecruiters"] == 1
    assert plan.target_limits_by_source["smartrecruiters"] == 1
    assert any(shard.source == "smartrecruiters" for shard in manifest.shards)


def test_workday_ramp_is_explicit_and_bounded():
    registry = inventory_refresh.load_production_registry(REGISTRY)

    for workday_limit in inventory_refresh.WORKDAY_RAMP_LEVELS:
        limits = inventory_refresh.default_refresh_limits(
            registry,
            workday_limit=workday_limit,
        )
        plan, subset, manifest = inventory_refresh.build_refresh_plan(
            registry=registry,
            cohort=17,
            limits=limits,
        )
        assert subset.target_counts_by_source["workday"] == workday_limit
        assert plan.target_limits_by_source["workday"] == workday_limit
        assert plan.total_targets <= inventory_refresh.MAX_REFRESH_TARGETS
        workday_shards = [
            shard for shard in manifest.shards if shard.source == "workday"
        ]
        assert len(workday_shards) == workday_limit
        assert all(len(shard.target_identities) == 1 for shard in workday_shards)


def test_workday_ramp_does_not_auto_escalate_with_cohort():
    registry = inventory_refresh.load_production_registry(REGISTRY)

    _plan0, subset0, _manifest0 = inventory_refresh.build_refresh_plan(
        registry=registry,
        cohort=0,
    )
    _plan_late, subset_late, _manifest_late = inventory_refresh.build_refresh_plan(
        registry=registry,
        cohort=24 * 30,
    )

    assert subset0.target_counts_by_source["workday"] == 1
    assert subset_late.target_counts_by_source["workday"] == 1


def test_workday_ramp_rejects_unapproved_level():
    registry = inventory_refresh.load_production_registry(REGISTRY)

    try:
        inventory_refresh.default_refresh_limits(registry, workday_limit=24)
    except ValueError as exc:
        assert "Workday refresh limit must be one of" in str(exc)
    else:
        raise AssertionError("unapproved Workday ramp level was accepted")


def test_refresh_plan_records_guarded_workday_detail_concurrency():
    registry = inventory_refresh.load_production_registry(REGISTRY)
    limits = inventory_refresh.default_refresh_limits(registry, workday_limit=10)

    plan, _subset, _manifest = inventory_refresh.build_refresh_plan(
        registry=registry,
        cohort=17,
        limits=limits,
        workday_detail_concurrency=6,
    )

    assert plan.workday_detail_concurrency == 6


def test_refresh_plan_freezes_workday_brief_bindings():
    registry = inventory_refresh.load_production_registry(REGISTRY)
    binding = inventory_refresh.WorkdayBriefBinding(
        path="config/search_briefs/example.json",
        sha256="a" * 64,
    )

    plan, _subset, _manifest = inventory_refresh.build_refresh_plan(
        registry=registry,
        cohort=17,
        workday_briefs=[binding],
    )

    assert plan.workday_briefs == [binding]


def test_production_refresh_ceiling_is_independent_from_benchmark_ceiling():
    from job_scout.shard_benchmark import MAX_BOUNDED_TARGETS, select_registry_subset

    registry = inventory_refresh.load_production_registry(REGISTRY)
    limits = inventory_refresh.default_refresh_limits(registry, workday_limit=25)
    plan, subset, _manifest = inventory_refresh.build_refresh_plan(
        registry=registry,
        cohort=17,
        limits=limits,
    )

    assert MAX_BOUNDED_TARGETS == 100
    assert plan.total_targets == 124
    assert len(subset.targets) == 124
    try:
        select_registry_subset(registry, target_limits_by_source=limits)
    except ValueError as exc:
        assert "bounded benchmark is limited to 100 total targets" in str(exc)
    else:
        raise AssertionError("benchmark ceiling was silently widened")


def test_refresh_collector_consumes_refresh_plan_without_benchmark_schema(monkeypatch):
    registry = inventory_refresh.load_production_registry(REGISTRY)
    limits = inventory_refresh.default_refresh_limits(registry, workday_limit=25)
    plan, subset, manifest = inventory_refresh.build_refresh_plan(
        registry=registry,
        cohort=17,
        limits=limits,
    )
    captured = {}
    sentinel = object()

    def fake_collect_shard(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(inventory_refresh, "collect_shard", fake_collect_shard)
    result = inventory_refresh.collect_refresh_shard(
        registry=subset,
        manifest=manifest,
        plan=plan,
        shard_id=manifest.shards[0].shard_id,
    )

    assert result is sentinel
    assert captured["registry"] is subset
    assert captured["manifest"] is manifest
    assert captured["shard_id"] == manifest.shards[0].shard_id


def test_refresh_collector_rejects_plan_from_different_registry(monkeypatch):
    registry = inventory_refresh.load_production_registry(REGISTRY)
    plan, subset, manifest = inventory_refresh.build_refresh_plan(
        registry=registry,
        cohort=17,
    )
    payload = plan.model_dump(mode="json")
    payload["refresh_registry_sha256"] = "0" * 64
    bad_plan = inventory_refresh.RefreshPlan.model_validate(payload)
    monkeypatch.setattr(
        inventory_refresh,
        "collect_shard",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("collector must not run")),
    )

    try:
        inventory_refresh.collect_refresh_shard(
            registry=subset,
            manifest=manifest,
            plan=bad_plan,
            shard_id=manifest.shards[0].shard_id,
        )
    except ValueError as exc:
        assert "refresh plan does not match registry/manifest" in str(exc)
    else:
        raise AssertionError("mismatched refresh plan was accepted")


def test_live_refresh_workflow_uses_refresh_collector_contract():
    workflow = Path(".github/workflows/refresh-live-inventory.yml").read_text(
        encoding="utf-8"
    )

    assert "python -m job_scout.inventory_refresh collect" in workflow
    assert "--refresh-plan refresh-plan/plan.json" in workflow
    assert "--repo-root ." in workflow
    assert "--workday-detail-concurrency" in workflow
    assert "inputs.workday_detail_concurrency" in workflow
    assert '--database "$JOBSIFT_DATABASE"' in workflow
    assert "--plan-dir config/sourcing_plans" in workflow
    assert "TURSO_DATABASE_URL" in workflow
    assert workflow.count("group: jobsift-client-delivery-mutation") == 1
    assert workflow.count("queue: max") == 2
    assert "group: jobsift-live-inventory-refresh" in workflow
    persist_job = workflow.index("persist-and-deliver:")
    mutation_lock = workflow.index("group: jobsift-client-delivery-mutation")
    verify_snapshot = workflow.index("Verify active profile snapshot")
    fan_in = workflow.index("python -m job_scout.inventory_refresh fan-in")
    deliver = workflow.index("Deliver to active client profiles")
    assert "profile_mutation_lock" not in workflow
    assert persist_job < mutation_lock < verify_snapshot < fan_in < deliver
    assert "verify-profile-snapshot" in workflow
    assert "--parent-registry refresh-plan/parent-registry.json" in workflow
    assert "--refresh-plan refresh-plan/plan.json" in workflow[fan_in:deliver]
    assert "job_scout.shard_benchmark collect" not in workflow
    assert "--benchmark-plan refresh-plan/plan.json" not in workflow


def test_live_refresh_delivers_before_retention_cleanup():
    workflow = Path(".github/workflows/refresh-live-inventory.yml").read_text(
        encoding="utf-8"
    )

    fan_in = workflow.index("python -m job_scout.inventory_refresh fan-in")
    deliver = workflow.index("Deliver to active client profiles")
    complete = workflow.index("Mark scheduled cohort complete")
    prune_step = workflow.index("Prune stale inventory")
    prune = workflow.index("python -m job_scout.inventory_refresh prune")

    assert "--skip-retention" in workflow[fan_in:deliver]
    assert fan_in < deliver < complete < prune
    assert "if: ${{ always() }}" in workflow[prune_step:prune]


def test_live_refresh_workflow_enforces_logical_scheduler_contract():
    workflow = Path(".github/workflows/refresh-live-inventory.yml").read_text(
        encoding="utf-8"
    )

    resolver = workflow.index("Resolve logical refresh cohort")
    build = workflow.index("Build rotating refresh cohort")
    collect = workflow.index("  collect:")
    persist = workflow.index("  persist-and-deliver:")
    complete = workflow.index("Mark scheduled cohort complete")
    prune = workflow.index("Prune stale inventory")

    scheduler_block = workflow[resolver:build]
    completion_block = workflow[complete:prune]

    assert '- cron: "2,17,32,47 * * * *"' in workflow
    assert workflow.count("group: jobsift-live-inventory-refresh") == 1
    assert "python -m job_scout.inventory_refresh schedule-next" in scheduler_block
    assert "mkdir -p refresh-plan" in scheduler_block
    assert "--output refresh-plan/schedule.json" in scheduler_block
    assert 'trigger": "workflow_dispatch"' in scheduler_block
    assert "cohort=$(( $(date -u +%s) / 3600 ))" in scheduler_block
    assert "should_run: ${{ steps.matrix.outputs.should_run }}" in workflow
    assert "cohort: ${{ steps.schedule.outputs.cohort }}" in workflow
    assert "matrix='{\"include\":[]}'" in workflow
    assert "name: jobsift-refresh-plan-${{ github.run_id }}" in workflow
    assert "path: refresh-plan/" in workflow
    assert "if-no-files-found: error" in workflow
    assert "if: ${{ needs.plan.outputs.should_run == 'true' }}" in workflow[collect:persist]
    assert "needs.plan.outputs.should_run == 'true'" in workflow[persist:complete]
    assert "if: ${{ github.event_name == 'schedule' }}" in completion_block
    assert '--cohort "${{ needs.plan.outputs.cohort }}"' in completion_block
    assert "--fan-in-report refresh-result/report.json" in completion_block
    assert "schedule-complete" not in scheduler_block


def test_profile_mutation_workflows_queue_all_pending_changes():
    for path in (
        Path(".github/workflows/client-delivery-control.yml"),
        Path(".github/workflows/configure-client-delivery-profile.yml"),
    ):
        workflow = path.read_text(encoding="utf-8")
        assert "group: jobsift-client-delivery-mutation" in workflow
        assert "queue: max" in workflow
        assert "cancel-in-progress: false" in workflow


def test_delivery_profile_cli_restricts_remote_mutations_to_queued_workflows():
    cli = Path("job_scout/cli.py").read_text(encoding="utf-8")
    assert "_require_serialized_profile_mutation" in cli
    assert "SERIALIZED_PROFILE_MUTATION_WORKFLOWS" in cli
    assert "Client Delivery Control" in cli
    assert "Configure Client Delivery Profile" in cli
    assert "profile_mutation_guard" not in cli


def test_legacy_refresh_plan_without_workday_snapshot_uses_full_collector(monkeypatch):
    registry = inventory_refresh.load_production_registry(REGISTRY)
    plan, subset, manifest = inventory_refresh.build_refresh_plan(
        registry=registry,
        cohort=17,
        workday_briefs=None,
    )
    assert plan.workday_briefs is None

    captured = {}
    sentinel = object()

    def fake_collect_shard(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(inventory_refresh, "collect_shard", fake_collect_shard)
    result = inventory_refresh.collect_refresh_shard(
        registry=subset,
        manifest=manifest,
        plan=plan,
        shard_id=next(
            shard.shard_id for shard in manifest.shards if shard.source == "workday"
        ),
    )
    assert result is sentinel
    collector = captured["collector_factory"]("workday")
    assert collector.__class__.__name__ == "WorkdayCollector"
    collector.client.close()


def test_explicit_empty_workday_snapshot_remains_distinct_from_legacy():
    registry = inventory_refresh.load_production_registry(REGISTRY)
    plan, _subset, _manifest = inventory_refresh.build_refresh_plan(
        registry=registry,
        cohort=17,
        workday_briefs=[],
    )
    assert plan.workday_briefs == []

    payload = plan.model_dump(mode="json")
    payload.pop("workday_briefs")
    legacy = inventory_refresh.RefreshPlan.model_validate(payload)
    assert legacy.workday_briefs is None


def test_refresh_plan_carries_retained_workday_candidate_snapshot():
    registry = inventory_refresh.load_production_registry(REGISTRY)
    _legacy, subset, _manifest = inventory_refresh.build_refresh_plan(
        registry=registry,
        cohort=17,
    )
    board_id = next(
        target.source_target().board_id
        for target in subset.targets
        if target.source == "workday"
    )
    retained = inventory_refresh.WorkdayRetainedCandidateBinding(
        board_id=board_id,
        source_job_ids=["R123"],
    )

    plan, _subset, _manifest = inventory_refresh.build_refresh_plan(
        registry=registry,
        cohort=17,
        workday_briefs=[],
        workday_retained_candidates=[retained],
    )

    assert plan.workday_retained_candidates == [retained]


def test_index_first_plan_without_retained_snapshot_falls_back_to_full_workday(monkeypatch):
    registry = inventory_refresh.load_production_registry(REGISTRY)
    plan, subset, manifest = inventory_refresh.build_refresh_plan(
        registry=registry,
        cohort=17,
        workday_briefs=[],
        workday_retained_candidates=None,
    )
    captured = {}
    sentinel = object()

    def fake_collect_shard(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(inventory_refresh, "collect_shard", fake_collect_shard)
    result = inventory_refresh.collect_refresh_shard(
        registry=subset,
        manifest=manifest,
        plan=plan,
        shard_id=next(
            shard.shard_id for shard in manifest.shards if shard.source == "workday"
        ),
    )

    assert result is sentinel
    collector = captured["collector_factory"]("workday")
    assert collector.__class__.__name__ == "WorkdayCollector"
    collector.client.close()


def test_retained_workday_invalidation_identities_are_authoritative_db_keys():
    plan = inventory_refresh.RefreshPlan(
        parent_registry_id="parent",
        parent_registry_sha256="a" * 64,
        refresh_registry_id="refresh",
        refresh_registry_sha256="b" * 64,
        shard_manifest_sha256="c" * 64,
        target_limits_by_source={"workday": 1},
        shard_counts_by_source={"workday": 1},
        target_counts_by_source={"workday": 1},
        total_targets=1,
        total_shards=1,
        workday_detail_concurrency=4,
        workday_briefs=[],
        workday_retained_candidates=[
            inventory_refresh.WorkdayRetainedCandidateBinding(
                board_id="host:tenant:site",
                source_job_ids=["REQ-2", "REQ-1", "REQ-1"],
            )
        ],
        matrix={"include": [{"shard_id": "workday-000", "source": "workday"}]},
    )

    assert inventory_refresh._retained_workday_invalidation_identities(plan) == [
        ("workday", "host:tenant:site", "REQ-1"),
        ("workday", "host:tenant:site", "REQ-2"),
    ]


def test_fan_in_parent_registry_snapshot_mismatch_fails_closed():
    registry = inventory_refresh.load_production_registry(REGISTRY)
    plan, _subset, _manifest = inventory_refresh.build_refresh_plan(
        registry=registry,
        cohort=0,
    )
    mismatched = registry.model_copy(
        update={"registry_id": f"{registry.registry_id}-mismatch"}
    )

    try:
        inventory_refresh.validate_parent_registry_snapshot(
            parent_registry=mismatched,
            refresh_plan=plan,
        )
    except ValueError as exc:
        assert "parent registry does not match refresh-plan snapshot" in str(exc)
    else:
        raise AssertionError("mismatched parent registry was accepted")
