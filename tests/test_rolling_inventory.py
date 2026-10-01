from pathlib import Path

import pytest

from job_scout.production_registry import load_production_registry
from job_scout.rolling_inventory import DEFAULT_LIMITS, build_refresh_registry


REGISTRY = Path("config/source_registries/production_active_v1.json")


def test_rotating_refresh_keeps_bounded_provider_mix_and_changes_each_slot():
    registry = load_production_registry(REGISTRY)

    first = build_refresh_registry(registry, slot=0)
    second = build_refresh_registry(registry, slot=1)

    assert len(first.targets) == sum(DEFAULT_LIMITS.values()) == 100
    assert first.target_counts_by_source == dict(sorted(DEFAULT_LIMITS.items()))
    assert second.target_counts_by_source == first.target_counts_by_source

    first_ids = {target.target_identity for target in first.targets}
    second_ids = {target.target_identity for target in second.targets}
    assert first_ids != second_ids

    for source, count in DEFAULT_LIMITS.items():
        left = {target.target_identity for target in first.targets if target.source == source}
        right = {target.target_identity for target in second.targets if target.source == source}
        assert len(left) == count
        assert len(right) == count
        assert left.isdisjoint(right)


def test_rotating_refresh_rejects_invalid_slots():
    registry = load_production_registry(REGISTRY)
    with pytest.raises(ValueError, match="non-negative"):
        build_refresh_registry(registry, slot=-1)
