from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from job_scout.production_registry import (
    ProductionSourceRegistry,
    build_production_registry,
    build_shard_manifest,
    load_production_registry,
)


def universe_and_health():
    universe = {
        "target_records": [
            {
                "target_identity": "greenhouse:a",
                "source": "greenhouse",
                "coordinates": {"board": "a"},
                "company_hint": "A",
            },
            {
                "target_identity": "greenhouse:b",
                "source": "greenhouse",
                "coordinates": {"board": "b"},
                "company_hint": "B",
            },
            {
                "target_identity": "lever:global:c",
                "source": "lever",
                "coordinates": {"instance": "global", "site": "c"},
                "company_hint": "C",
            },
        ]
    }
    health = {
        "manifest_sha256": "a" * 64,
        "updated_at": "2026-09-09T05:13:04+00:00",
        "results": [
            {
                "target_identity": "greenhouse:a",
                "source": "greenhouse",
                "classification": "active",
                "current_postings": 10,
                "inventory_exact": True,
                "manifest_sha256": "a" * 64,
            },
            {
                "target_identity": "greenhouse:b",
                "source": "greenhouse",
                "classification": "valid_empty",
                "current_postings": 0,
                "inventory_exact": True,
                "manifest_sha256": "a" * 64,
            },
            {
                "target_identity": "lever:global:c",
                "source": "lever",
                "classification": "active",
                "current_postings": 55,
                "inventory_exact": True,
                "manifest_sha256": "a" * 64,
            },
        ],
    }
    return universe, health


def test_registry_approves_only_active_targets() -> None:
    universe, health = universe_and_health()
    registry = build_production_registry(
        universe=universe,
        health=health,
        target_universe_git_blob_sha="b" * 40,
        registry_id="test-v1",
    )

    assert [target.target_identity for target in registry.targets] == [
        "greenhouse:a",
        "lever:global:c",
    ]
    assert registry.target_counts_by_source == {"greenhouse": 1, "lever": 1}
    assert registry.approval_policy == "health-active-only"


def test_registry_fails_closed_on_incomplete_or_mismatched_health() -> None:
    universe, health = universe_and_health()
    health["results"].pop()
    with pytest.raises(ValueError, match="health/universe target mismatch"):
        build_production_registry(
            universe=universe,
            health=health,
            target_universe_git_blob_sha="b" * 40,
            registry_id="test-v1",
        )

    universe, health = universe_and_health()
    health["results"][0]["source"] = "ashby"
    with pytest.raises(ValueError, match="health source mismatch"):
        build_production_registry(
            universe=universe,
            health=health,
            target_universe_git_blob_sha="b" * 40,
            registry_id="test-v1",
        )


def test_registry_requires_canonical_target_order() -> None:
    universe, health = universe_and_health()
    registry = build_production_registry(
        universe=universe,
        health=health,
        target_universe_git_blob_sha="b" * 40,
        registry_id="test-v1",
    )
    payload = registry.model_dump(mode="json")
    payload["targets"] = list(reversed(payload["targets"]))
    with pytest.raises(ValueError, match="sorted by identity"):
        ProductionSourceRegistry.model_validate(payload)


def test_provider_shards_are_deterministic_balanced_and_exhaustive() -> None:
    universe, health = universe_and_health()
    for index in range(2, 8):
        universe["target_records"].append(
            {
                "target_identity": f"greenhouse:g{index}",
                "source": "greenhouse",
                "coordinates": {"board": f"g{index}"},
                "company_hint": None,
            }
        )
        health["results"].append(
            {
                "target_identity": f"greenhouse:g{index}",
                "source": "greenhouse",
                "classification": "active",
                "current_postings": index,
                "inventory_exact": True,
                "manifest_sha256": "a" * 64,
            }
        )

    registry = build_production_registry(
        universe=universe,
        health=health,
        target_universe_git_blob_sha="b" * 40,
        registry_id="test-v1",
    )
    first = build_shard_manifest(
        registry, shard_counts_by_source={"greenhouse": 3, "lever": 1}
    )
    second = build_shard_manifest(
        registry, shard_counts_by_source={"greenhouse": 3, "lever": 1}
    )

    assert first == second
    ids = [identity for shard in first.shards for identity in shard.target_identities]
    assert sorted(ids) == [target.target_identity for target in registry.targets]
    assert all(
        all(identity.startswith(f"{shard.source}:") for identity in shard.target_identities)
        for shard in first.shards
    )
    greenhouse_sizes = [
        len(shard.target_identities) for shard in first.shards if shard.source == "greenhouse"
    ]
    assert max(greenhouse_sizes) - min(greenhouse_sizes) <= 1


def test_checked_production_registry_is_exact_active_health_set() -> None:
    path = Path("config/source_registries/production_active_v1.json")
    registry = load_production_registry(path)

    assert registry.registry_id == "ats-active-2026-09-v1"
    assert len(registry.targets) == 2044
    assert registry.target_counts_by_source == {
        "ashby": 607,
        "greenhouse": 665,
        "lever": 236,
        "workday": 536,
    }
    assert all(target.health_classification == "active" for target in registry.targets)
    assert registry.health_manifest_sha256 == (
        "9e8fe896d2217d0212400ffc78bdb70c414f53a3ccadf868bc2734ea56acb8a4"
    )
