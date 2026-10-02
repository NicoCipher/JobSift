from pathlib import Path

from job_scout import inventory_refresh

REGISTRY = Path("config/source_registries/production_active_v1.json")


def test_rotating_refresh_plan_is_bounded_and_changes_cohort():
    registry = inventory_refresh.load_production_registry(REGISTRY)

    plan0, subset0, manifest0 = inventory_refresh.build_refresh_plan(
        registry=registry, cohort=0
    )
    _plan1, subset1, manifest1 = inventory_refresh.build_refresh_plan(
        registry=registry, cohort=1
    )

    assert len(subset0.targets) == sum(v for k, v in inventory_refresh.DEFAULT_LIMITS.items() if k in registry.target_counts_by_source) == 100
    assert subset0.target_counts_by_source == {k: v for k, v in inventory_refresh.DEFAULT_LIMITS.items() if k in registry.target_counts_by_source}
    assert plan0.total_targets == 100
    assert len(manifest0.shards) == plan0.total_shards
    assert [target.target_identity for target in subset0.targets] == sorted(
        target.target_identity for target in subset0.targets
    )

    first = {target.target_identity for target in subset0.targets}
    second = {target.target_identity for target in subset1.targets}
    assert first != second
    assert subset1.target_counts_by_source == {k: v for k, v in inventory_refresh.DEFAULT_LIMITS.items() if k in registry.target_counts_by_source}
    assert manifest1.registry_id == subset1.registry_id


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
        expected_workday_shards = min(
            workday_limit,
            max(1, (workday_limit + 4) // 5),
        )
        assert len(
            [shard for shard in manifest.shards if shard.source == "workday"]
        ) == expected_workday_shards


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
    assert "job_scout.shard_benchmark collect" not in workflow
    assert "--benchmark-plan refresh-plan/plan.json" not in workflow


def test_live_refresh_delivers_before_retention_cleanup():
    workflow = Path(".github/workflows/refresh-live-inventory.yml").read_text(
        encoding="utf-8"
    )

    fan_in = workflow.index("python -m job_scout.inventory_refresh fan-in")
    deliver = workflow.index("Deliver to active client profiles")
    prune = workflow.index("python -m job_scout.inventory_refresh prune")

    assert "--skip-retention" in workflow[fan_in:deliver]
    assert fan_in < deliver < prune
