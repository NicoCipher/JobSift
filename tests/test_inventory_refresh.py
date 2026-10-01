from pathlib import Path

import job_scout.inventory_refresh as inventory_refresh
from job_scout.production_registry import load_production_registry


REGISTRY = Path("config/source_registries/production_active_v1.json")


def test_rotating_refresh_plan_is_bounded_and_changes_cohort():
    registry = load_production_registry(REGISTRY)

    plan0, subset0, manifest0 = inventory_refresh.build_refresh_plan(
        registry=registry, cohort=0
    )
    _plan1, subset1, manifest1 = inventory_refresh.build_refresh_plan(
        registry=registry, cohort=1
    )

    assert len(subset0.targets) == sum(inventory_refresh.DEFAULT_LIMITS.values()) == 100
    assert subset0.target_counts_by_source == inventory_refresh.DEFAULT_LIMITS
    assert plan0.total_targets == 100
    assert len(manifest0.shards) == plan0.total_shards
    assert [target.target_identity for target in subset0.targets] == sorted(
        target.target_identity for target in subset0.targets
    )

    first = {target.target_identity for target in subset0.targets}
    second = {target.target_identity for target in subset1.targets}
    assert first != second
    assert subset1.target_counts_by_source == inventory_refresh.DEFAULT_LIMITS
    assert manifest1.registry_id == subset1.registry_id
