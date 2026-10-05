from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from job_scout import inventory_refresh
from job_scout.domain.models import Job, RemoteStatus
from job_scout.normalization.core import content_fingerprint
from job_scout.production_registry import ProductionTarget
from job_scout.search_brief import load_search_brief
from job_scout.storage.inventory_runs import InventoryRunStore
from job_scout.storage.sqlite import SQLiteRepository
from job_scout.yield_scheduling import TargetYieldSignal, load_target_yield_state


REGISTRY = Path("config/source_registries/production_active_v1.json")
BRIEF = Path("config/search_briefs/taiwo_software_remote_us_v1.json")


def _small_greenhouse_registry(count: int = 12):
    base = inventory_refresh.load_production_registry(REGISTRY)
    targets = [
        ProductionTarget(
            source="greenhouse",
            target_identity=f"greenhouse:yield-{index}",
            coordinates={"board": f"yield-{index}"},
            company_hint=f"Yield {index}",
        )
        for index in range(count)
    ]
    return base.model_copy(
        update={
            "registry_id": "yield-aware-test",
            "targets": targets,
            "target_counts_by_source": {"greenhouse": count},
        }
    )


def _job(
    job_id: str,
    *,
    title: str,
    country: str,
    remote: RemoteStatus,
    posted_at: datetime,
) -> Job:
    description = f"{title} role"
    return Job(
        id=job_id,
        source="greenhouse",
        source_job_id=job_id,
        source_board_id="yield-0",
        title=title,
        company="Yield 0",
        employer_id="yield-0",
        description_text=description,
        job_url=f"https://example.test/{job_id}",
        canonical_url=f"https://example.test/{job_id}",
        country=country,
        eligible_countries={country},
        location_text=f"{country} remote",
        remote_status=remote,
        posted_at=posted_at,
        discovered_at=posted_at,
        last_seen_at=posted_at,
        content_fingerprint=content_fingerprint(
            title=title,
            description=description,
            location=f"{country} remote",
            employment_type=None,
        ),
    )


def test_yield_state_counts_only_unique_fresh_client_matches(tmp_path):
    registry = _small_greenhouse_registry(1)
    target = registry.targets[0]
    repo = SQLiteRepository(tmp_path / "yield.sqlite3")
    store = InventoryRunStore(repo)
    observed_at = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

    jobs = [
        _job(
            "eligible",
            title="Software Engineer",
            country="United States",
            remote=RemoteStatus.REMOTE,
            posted_at=observed_at - timedelta(hours=2),
        ),
        _job(
            "wrong-title",
            title="Accountant",
            country="United States",
            remote=RemoteStatus.REMOTE,
            posted_at=observed_at - timedelta(hours=1),
        ),
        _job(
            "wrong-country",
            title="Software Engineer",
            country="India",
            remote=RemoteStatus.REMOTE,
            posted_at=observed_at - timedelta(hours=1),
        ),
        _job(
            "stale",
            title="Software Engineer",
            country="United States",
            remote=RemoteStatus.REMOTE,
            posted_at=observed_at - timedelta(hours=30),
        ),
    ]

    for run_id, completed_at in (
        ("run-1", observed_at),
        ("run-2", observed_at + timedelta(hours=1)),
    ):
        store.create(run_id=run_id, plan_id="plan", started_at=completed_at)
        store.persist_jobs_and_finish(
            run_id=run_id,
            jobs=jobs,
            memberships=[(target.target_identity, job) for job in jobs],
            target_observations=[
                (
                    target.target_identity,
                    target.source,
                    "success",
                    completed_at,
                    completed_at,
                    100,
                    len(jobs),
                    len(jobs),
                    len(jobs),
                    3,
                )
            ],
            status="success",
            completed_at=completed_at,
        )

    state = load_target_yield_state(
        repo,
        registry,
        [load_search_brief(BRIEF)],
        now=observed_at + timedelta(hours=2),
    )
    signal = state[target.target_identity]

    assert signal.observations == 2
    assert signal.fresh_jobs == 3
    assert signal.eligible_fresh_jobs == 1
    assert signal.eligible_observations == 1
    assert signal.last_eligible_at == observed_at


def test_yield_priority_adds_capacity_without_displacing_oldest_due_targets():
    registry = _small_greenhouse_registry()
    epoch = datetime(2026, 10, 1, tzinfo=UTC)
    selection_state = {
        target.target_identity: {
            "last_observed_at": None,
            "first_discovered_at": epoch.isoformat(),
        }
        for target in registry.targets
    }
    fairness = inventory_refresh.history_aware_registry(
        registry,
        cohort=7,
        limits={"greenhouse": 2},
        selection_state=selection_state,
    )
    fairness_ids = {target.target_identity for target in fairness.targets}
    priority = next(
        target for target in registry.targets if target.target_identity not in fairness_ids
    )
    yield_state = {
        priority.target_identity: TargetYieldSignal(
            target_identity=priority.target_identity,
            source="greenhouse",
            observations=2,
            fresh_jobs=4,
            eligible_fresh_jobs=2,
            eligible_observations=2,
            last_eligible_at=datetime(2026, 10, 5, tzinfo=UTC),
        )
    }

    plan, selected, _manifest = inventory_refresh.build_refresh_plan(
        registry=registry,
        cohort=7,
        limits={"greenhouse": 2},
        selection_state=selection_state,
        yield_state=yield_state,
        yield_extra_budget=1,
    )
    selected_ids = {target.target_identity for target in selected.targets}

    assert fairness_ids <= selected_ids
    assert priority.target_identity in selected_ids
    assert plan.selection_strategy == "yield-aware-oldest-due-v1"
    assert plan.fairness_floor_by_source == {"greenhouse": 2}
    assert plan.yield_priority_targets_by_source == {"greenhouse": 1}
    assert plan.target_counts_by_source == {"greenhouse": 3}
    assert plan.total_targets == 3


def test_yield_priority_never_forces_zero_yield_targets():
    registry = _small_greenhouse_registry()
    epoch = datetime(2026, 10, 1, tzinfo=UTC)
    selection_state = {
        target.target_identity: {
            "last_observed_at": None,
            "first_discovered_at": epoch.isoformat(),
        }
        for target in registry.targets
    }
    fairness = inventory_refresh.history_aware_registry(
        registry,
        cohort=2,
        limits={"greenhouse": 2},
        selection_state=selection_state,
    )
    zero_state = {
        target.target_identity: TargetYieldSignal(
            target_identity=target.target_identity,
            source="greenhouse",
            observations=3,
            fresh_jobs=10,
            eligible_fresh_jobs=0,
            eligible_observations=0,
        )
        for target in registry.targets
    }

    plan, selected, _manifest = inventory_refresh.build_refresh_plan(
        registry=registry,
        cohort=2,
        limits={"greenhouse": 2},
        selection_state=selection_state,
        yield_state=zero_state,
        yield_extra_budget=100,
    )

    assert {
        target.target_identity for target in selected.targets
    } == {
        target.target_identity for target in fairness.targets
    }
    assert plan.yield_priority_targets_by_source == {}
    assert plan.total_targets == 2
