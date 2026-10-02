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
