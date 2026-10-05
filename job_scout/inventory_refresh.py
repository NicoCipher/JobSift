"""Scheduled rotating refresh of the production-approved shared job inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from collections import Counter
from datetime import UTC, datetime
from functools import partial
from itertools import pairwise
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from job_scout.collectors.greenhouse import GreenhouseCollector
from job_scout.collectors.smartrecruiters import SmartRecruitersCollector
from job_scout.collectors.workday import WorkdayCollector
from job_scout.incremental_collection import (
    INCREMENTAL_SOURCES,
    IncrementalTargetState,
    snapshot_incremental_target_state,
)
from job_scout.production_registry import (
    CollectionShardManifest,
    ProductionSourceRegistry,
    build_shard_manifest,
    load_production_registry,
    sha256_json,
)
from job_scout.shard_benchmark import PROVIDERS
from job_scout.shard_collection import (
    ShardCollectionArtifact,
    collect_shard,
    default_collector_factory,
)
from job_scout.shard_fanin import persist_shard_artifacts
from job_scout.source_discovery import overlay_admitted_targets
from job_scout.storage.factory import create_repository
from job_scout.storage.inventory_runs import InventoryRunStore
from job_scout.storage.refresh_schedule import InventoryRefreshScheduleStore
from job_scout.storage.source_discovery import SourceDiscoveryStore
from job_scout.workday_production import (
    IndexFirstWorkdayCollector,
    WorkdayBriefBinding,
    WorkdayRetainedCandidateBinding,
    load_bound_workday_briefs,
    resolve_active_workday_brief_bindings,
    resolve_retained_workday_candidate_bindings,
    verify_active_workday_brief_bindings,
)
from job_scout.yield_scheduling import (
    YIELD_PRIORITY_EXTRA_BUDGET,
    TargetYieldSignal,
    load_target_yield_state,
    yield_priority_targets,
)

# Backward-compatible module attribute for older callers/tests; runtime uses create_repository.
SQLiteRepository = create_repository

DEFAULT_LIMITS = {
    "greenhouse": 40,
    "ashby": 40,
    "workday": 1,
    "lever": 19,
    "smartrecruiters": 7,
}
DEFAULT_SHARDS = {
    "greenhouse": 6,
    "ashby": 6,
    "workday": 1,
    "lever": 4,
    "smartrecruiters": 2,
}
MAX_TARGETS_PER_SHARD = {
    "greenhouse": 20,
    "ashby": 20,
    "workday": 1,
    "lever": 10,
    "smartrecruiters": 10,
}
MAX_REFRESH_SHARDS = 128
DEFAULT_WORKDAY_DETAIL_CONCURRENCY = 4
# GitHub's scheduled triggers have been materially less frequent than the logical
# hourly cadence in production. Keep fast ATS providers on a six-cohort coverage
# horizon so the live registry can still be swept within roughly one day even
# when only a handful of scheduled runs actually start.
FAST_PROVIDER_COVERAGE_COHORTS = 6
MAX_REFRESH_TARGETS = 600
WORKDAY_RAMP_LEVELS = (1, 5, 10, 20, 25)


class ProviderCoverageProof(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    registry_targets: int = Field(ge=1)
    targets_per_cohort: int = Field(ge=1)
    selection_period_cohorts: int = Field(ge=1)
    visits_per_target_min: int = Field(ge=1)
    visits_per_target_max: int = Field(ge=1)
    max_revisit_gap_cohorts: int = Field(ge=1)
    worst_case_full_coverage_cohorts: int = Field(ge=1)
    all_targets_reached: bool
    deterministic_wraparound: bool


class CoverageProof(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registry_id: str
    registry_sha256: str
    cohort_sequence_assumption: str = (
        "consecutive cohort indices with unchanged provider limits"
    )
    providers: list[ProviderCoverageProof]
    all_targets_reached: bool


class TargetObservationTelemetry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_identity: str
    source: str
    status: str
    started_at: datetime
    completed_at: datetime
    runtime_ms: int = Field(ge=0)
    raw_postings_received: int = Field(ge=0)
    normalized_jobs: int = Field(ge=0)
    postings_with_trustworthy_timestamps: int = Field(ge=0)
    postings_at_most_24h_old: int = Field(ge=0)
    previous_completed_at: datetime | None = None
    revisit_hours: float | None = Field(default=None, ge=0)


class ProviderObservedCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    approved_registry_targets: int = Field(ge=1)
    targets_per_cohort: int = Field(ge=1)
    current_targets_attempted: int = Field(ge=0)
    current_targets_succeeded: int = Field(ge=0)
    current_targets_partial: int = Field(ge=0)
    current_targets_failed: int = Field(ge=0)
    raw_postings_received: int = Field(ge=0)
    normalized_jobs: int = Field(ge=0)
    postings_with_trustworthy_timestamps: int = Field(ge=0)
    timestamp_known_rate: float | None = Field(default=None, ge=0, le=1)
    postings_at_most_24h_old: int = Field(ge=0)
    approved_targets_observed_ever: int = Field(ge=0)
    approved_targets_never_observed: int = Field(ge=0)
    observed_coverage_rate: float = Field(ge=0, le=1)
    revisit_samples: int = Field(ge=0)
    p95_observed_revisit_hours: float | None = Field(default=None, ge=0)
    max_observed_revisit_hours: float | None = Field(default=None, ge=0)
    max_hours_since_last_observation: float | None = Field(default=None, ge=0)
    target_runtime_p50_ms: int = Field(ge=0)
    target_runtime_p95_ms: int = Field(ge=0)


class ObservedCoverageReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    parent_registry_id: str
    parent_registry_sha256: str
    selected_registry_id: str
    generated_at: datetime
    observation_history_started_at: datetime | None
    revisit_sample_scope: str = "current run compared with each target's prior observation"
    providers: list[ProviderObservedCoverage]
    targets: list[TargetObservationTelemetry]


class RefreshPlan(BaseModel):
    """Production refresh plan with a ceiling independent of benchmark limits."""

    model_config = ConfigDict(extra="forbid")

    parent_registry_id: str
    parent_registry_sha256: str
    refresh_registry_id: str
    refresh_registry_sha256: str
    shard_manifest_sha256: str
    target_limits_by_source: dict[str, int]
    shard_counts_by_source: dict[str, int]
    target_counts_by_source: dict[str, int]
    total_targets: int = Field(ge=1, le=MAX_REFRESH_TARGETS)
    total_shards: int = Field(ge=1, le=MAX_REFRESH_SHARDS)
    selection_strategy: str = "cohort-rotation-v1"
    fairness_floor_by_source: dict[str, int] | None = None
    yield_priority_targets_by_source: dict[str, int] = Field(default_factory=dict)
    yield_signal_sha256: str | None = None
    workday_detail_concurrency: int = Field(ge=1, le=8)
    # None is reserved for pre-index-first/legacy plans that did not freeze a brief snapshot.
    # New production plans always store a list, including [] when no profiles are active.
    workday_briefs: list[WorkdayBriefBinding] | None = None
    # None means the plan predates retained-candidate protection and must use
    # legacy full Workday collection rather than unsafe negative title pruning.
    workday_retained_candidates: list[WorkdayRetainedCandidateBinding] | None = None
    # None preserves legacy full-provider behavior. New production plans freeze
    # provider identity/freshness memory so workers can crawl incrementally
    # without direct production-database access.
    incremental_target_state: list[IncrementalTargetState] | None = None
    matrix: dict[str, list[dict[str, str]]]


def default_refresh_limits(
    registry: ProductionSourceRegistry,
    *,
    workday_limit: int | None = None,
) -> dict[str, int]:
    """Size fast-provider cohorts for broad freshness coverage, not old fixed caps.

    Greenhouse, Ashby, Lever, and SmartRecruiters are cheap enough to sweep much
    more aggressively than Workday. Their per-run limits therefore scale with the
    live approved registry so every target is selected within at most
    FAST_PROVIDER_COVERAGE_COHORTS logical cohorts. Existing historical limits
    remain floors for small registries.

    Workday stays on the explicit guarded ramp because large Workday boards can
    still dominate runtime even with index-first hydration.
    """

    limits: dict[str, int] = {}
    for source in PROVIDERS:
        count = registry.target_counts_by_source.get(source)
        if count is None:
            continue
        if source == "workday":
            requested = DEFAULT_LIMITS[source] if workday_limit is None else workday_limit
            if requested not in WORKDAY_RAMP_LEVELS:
                raise ValueError(
                    f"Workday refresh limit must be one of {WORKDAY_RAMP_LEVELS}"
                )
            limits[source] = min(requested, count)
            continue

        coverage_floor = math.ceil(count / FAST_PROVIDER_COVERAGE_COHORTS)
        limits[source] = min(
            count,
            max(DEFAULT_LIMITS[source], coverage_floor),
        )

    if sum(limits.values()) > MAX_REFRESH_TARGETS:
        raise ValueError(
            "freshness coverage cohort exceeds production safety ceiling; "
            "raise capacity deliberately or shorten the admitted registry"
        )
    return limits


def _selection_indices(*, count: int, limit: int, cohort: int) -> tuple[int, ...]:
    start = (cohort * limit) % count
    return tuple((start + offset) % count for offset in range(limit))


def _provider_coverage_proof(
    *, source: str, registry_targets: int, targets_per_cohort: int
) -> ProviderCoverageProof:
    if not 1 <= targets_per_cohort <= registry_targets:
        raise ValueError("coverage limit must be within provider registry size")
    period = registry_targets // math.gcd(registry_targets, targets_per_cohort)
    visits: list[list[int]] = [[] for _ in range(registry_targets)]
    for cohort in range(period):
        for index in _selection_indices(
            count=registry_targets,
            limit=targets_per_cohort,
            cohort=cohort,
        ):
            visits[index].append(cohort)
    all_targets_reached = all(visits)
    if not all_targets_reached:
        raise ValueError(f"refresh rotation starves {source} targets")

    revisit_gaps = []
    for target_visits in visits:
        for current, nxt in pairwise(target_visits):
            revisit_gaps.append(nxt - current)
        revisit_gaps.append(target_visits[0] + period - target_visits[-1])

    worst_coverage = 0
    for start_cohort in range(period):
        seen: set[int] = set()
        for step in range(1, period + 1):
            cohort = (start_cohort + step - 1) % period
            seen.update(
                _selection_indices(
                    count=registry_targets,
                    limit=targets_per_cohort,
                    cohort=cohort,
                )
            )
            if len(seen) == registry_targets:
                worst_coverage = max(worst_coverage, step)
                break
        else:
            raise ValueError(f"refresh rotation cannot cover {source} registry")

    return ProviderCoverageProof(
        source=source,
        registry_targets=registry_targets,
        targets_per_cohort=targets_per_cohort,
        selection_period_cohorts=period,
        visits_per_target_min=min(len(value) for value in visits),
        visits_per_target_max=max(len(value) for value in visits),
        max_revisit_gap_cohorts=max(revisit_gaps),
        worst_case_full_coverage_cohorts=worst_coverage,
        all_targets_reached=True,
        deterministic_wraparound=(
            _selection_indices(
                count=registry_targets,
                limit=targets_per_cohort,
                cohort=0,
            )
            == _selection_indices(
                count=registry_targets,
                limit=targets_per_cohort,
                cohort=period,
            )
        ),
    )


def build_coverage_proof(
    registry: ProductionSourceRegistry,
    *,
    limits: dict[str, int],
) -> CoverageProof:
    providers = tuple(
        source for source in PROVIDERS if source in registry.target_counts_by_source
    )
    if set(limits) != set(providers):
        raise ValueError("coverage limits must contain every approved provider")
    proofs = [
        _provider_coverage_proof(
            source=source,
            registry_targets=registry.target_counts_by_source[source],
            targets_per_cohort=limits[source],
        )
        for source in providers
    ]
    return CoverageProof(
        registry_id=registry.registry_id,
        registry_sha256=sha256_json(registry.model_dump(mode="json")),
        providers=proofs,
        all_targets_reached=all(value.all_targets_reached for value in proofs),
    )


def _percentile_int(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def _percentile_float(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 3)


def build_observed_coverage_report(
    *,
    repository: SQLiteRepository,
    run_id: str,
    parent_registry: ProductionSourceRegistry,
    selected_registry: ProductionSourceRegistry,
    generated_at: datetime,
) -> ObservedCoverageReport:
    # Query only the current run plus one compact row per known target. The full
    # observation ledger remains available for audit, but reporting stays bounded
    # as history grows.
    InventoryRunStore(repository)
    approved_by_source = {
        source: {
            target.target_identity
            for target in parent_registry.targets
            if target.source == source
        }
        for source in parent_registry.target_counts_by_source
    }
    selected_limits = selected_registry.target_counts_by_source
    if not set(selected_limits) <= set(approved_by_source):
        raise ValueError("selected refresh registry contains an unapproved provider")
    parent_targets = {target.target_identity for target in parent_registry.targets}
    if any(
        target.target_identity not in parent_targets
        for target in selected_registry.targets
    ):
        raise ValueError("selected refresh registry contains a target outside parent registry")

    normalized_generated_at = generated_at.replace(
        tzinfo=generated_at.tzinfo or UTC
    ).astimezone(UTC)
    with repository.connect() as connection:
        current_rows = connection.execute(
            "SELECT o.run_id,o.target_identity,o.source,o.started_at,o.completed_at,"
            "o.status,o.runtime_ms,o.raw_postings_received,o.normalized_jobs,"
            "o.postings_with_trustworthy_timestamps,o.postings_at_most_24h_old,"
            "(SELECT MAX(p.completed_at) FROM inventory_target_observations p "
            "WHERE p.target_identity=o.target_identity "
            "AND p.completed_at<o.completed_at) AS previous_completed_at "
            "FROM inventory_target_observations o WHERE o.run_id=? "
            "ORDER BY o.source,o.target_identity",
            (run_id,),
        ).fetchall()
        state_rows = connection.execute(
            "SELECT target_identity,source,first_observed_at,previous_observed_at,"
            "last_observed_at,observation_count,last_run_id "
            "FROM inventory_target_coverage_state "
            "ORDER BY source,target_identity"
        ).fetchall()
        relevant_state_rows = [
            row
            for row in state_rows
            if row["target_identity"] in parent_targets
        ]
        has_future_state = any(
            datetime.fromisoformat(row["last_observed_at"]) > normalized_generated_at
            for row in relevant_state_rows
        )
        if has_future_state:
            # Replay an older run against the observation ledger as it existed at
            # that run's persisted time. The compact state intentionally tracks
            # only the latest observation and can otherwise leak future coverage.
            as_of_rows = connection.execute(
                "SELECT target_identity,source,MIN(completed_at) AS first_observed_at,"
                "MAX(completed_at) AS last_observed_at "
                "FROM inventory_target_observations "
                "WHERE completed_at<=? "
                "GROUP BY target_identity,source "
                "ORDER BY source,target_identity",
                (normalized_generated_at.isoformat(),),
            ).fetchall()
            state_by_target = {
                row["target_identity"]: row
                for row in as_of_rows
                if row["target_identity"] in parent_targets
            }
        else:
            state_by_target = {
                row["target_identity"]: row for row in relevant_state_rows
            }

    first_observed = [
        datetime.fromisoformat(row["first_observed_at"])
        for row in state_by_target.values()
    ]
    history_started_at = min(first_observed) if first_observed else None

    target_reports: list[TargetObservationTelemetry] = []
    for row in current_rows:
        completed_at = datetime.fromisoformat(row["completed_at"])
        previous_completed_at = (
            datetime.fromisoformat(row["previous_completed_at"])
            if row["previous_completed_at"]
            else None
        )
        revisit_hours = (
            round((completed_at - previous_completed_at).total_seconds() / 3600, 3)
            if previous_completed_at is not None
            else None
        )
        target_reports.append(
            TargetObservationTelemetry(
                target_identity=row["target_identity"],
                source=row["source"],
                status=row["status"],
                started_at=datetime.fromisoformat(row["started_at"]),
                completed_at=completed_at,
                runtime_ms=row["runtime_ms"],
                raw_postings_received=row["raw_postings_received"],
                normalized_jobs=row["normalized_jobs"],
                postings_with_trustworthy_timestamps=row[
                    "postings_with_trustworthy_timestamps"
                ],
                postings_at_most_24h_old=row["postings_at_most_24h_old"],
                previous_completed_at=previous_completed_at,
                revisit_hours=revisit_hours,
            )
        )

    providers: list[ProviderObservedCoverage] = []
    for source in sorted(selected_limits):
        approved = approved_by_source[source]
        current = [row for row in target_reports if row.source == source]
        observed = approved.intersection(state_by_target)
        current_revisit_gaps = [
            row.revisit_hours
            for row in current
            if row.revisit_hours is not None
        ]
        last_observed = [
            datetime.fromisoformat(state_by_target[target_identity]["last_observed_at"])
            for target_identity in observed
        ]
        normalized_jobs = sum(row.normalized_jobs for row in current)
        timestamped = sum(
            row.postings_with_trustworthy_timestamps for row in current
        )
        runtimes = [row.runtime_ms for row in current]
        providers.append(
            ProviderObservedCoverage(
                source=source,
                approved_registry_targets=len(approved),
                targets_per_cohort=selected_limits[source],
                current_targets_attempted=len(current),
                current_targets_succeeded=sum(row.status == "success" for row in current),
                current_targets_partial=sum(row.status == "partial" for row in current),
                current_targets_failed=sum(
                    row.status not in {"success", "partial"} for row in current
                ),
                raw_postings_received=sum(
                    row.raw_postings_received for row in current
                ),
                normalized_jobs=normalized_jobs,
                postings_with_trustworthy_timestamps=timestamped,
                timestamp_known_rate=(
                    round(timestamped / normalized_jobs, 6)
                    if normalized_jobs
                    else None
                ),
                postings_at_most_24h_old=sum(
                    row.postings_at_most_24h_old for row in current
                ),
                approved_targets_observed_ever=len(observed),
                approved_targets_never_observed=len(approved) - len(observed),
                observed_coverage_rate=round(len(observed) / len(approved), 6),
                revisit_samples=len(current_revisit_gaps),
                p95_observed_revisit_hours=_percentile_float(
                    current_revisit_gaps, 0.95
                ),
                max_observed_revisit_hours=(
                    round(max(current_revisit_gaps), 3)
                    if current_revisit_gaps
                    else None
                ),
                max_hours_since_last_observation=(
                    round(
                        max(
                            (
                                normalized_generated_at
                                - value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)
                            ).total_seconds()
                            / 3600
                            for value in last_observed
                        ),
                        3,
                    )
                    if last_observed
                    else None
                ),
                target_runtime_p50_ms=_percentile_int(runtimes, 0.50),
                target_runtime_p95_ms=_percentile_int(runtimes, 0.95),
            )
        )

    return ObservedCoverageReport(
        run_id=run_id,
        parent_registry_id=parent_registry.registry_id,
        parent_registry_sha256=sha256_json(parent_registry.model_dump(mode="json")),
        selected_registry_id=selected_registry.registry_id,
        generated_at=normalized_generated_at,
        observation_history_started_at=history_started_at,
        providers=providers,
        targets=target_reports,
    )

def load_target_selection_state(
    repository,
    registry: ProductionSourceRegistry,
) -> dict[str, dict[str, str | None]]:
    """Load durable fairness timestamps for the current approved registry.

    A target's due timestamp is its last successful/partial observation when one
    exists, otherwise its first discovery time. Static registry targets that
    predate live discovery have no discovery row and are treated as oldest.
    """

    InventoryRunStore(repository)
    approved = {target.target_identity for target in registry.targets}
    state = {
        target_identity: {
            "last_observed_at": None,
            "first_discovered_at": None,
        }
        for target_identity in approved
    }
    with repository.connect() as connection:
        observed_rows = connection.execute(
            "SELECT target_identity,last_observed_at "
            "FROM inventory_target_coverage_state"
        ).fetchall()
        discovered_rows = connection.execute(
            "SELECT target_identity,first_discovered_at "
            "FROM source_discovery_targets"
        ).fetchall()

    for row in observed_rows:
        target_identity = row["target_identity"]
        if target_identity in state:
            state[target_identity]["last_observed_at"] = row["last_observed_at"]
    for row in discovered_rows:
        target_identity = row["target_identity"]
        if target_identity in state:
            state[target_identity]["first_discovered_at"] = row[
                "first_discovered_at"
            ]
    return state


def _selection_due_key(
    target,
    selection_state: dict[str, dict[str, str | None]],
) -> tuple[datetime, str, str]:
    value = selection_state.get(target.target_identity, {})
    raw_due = value.get("last_observed_at") or value.get("first_discovered_at")
    if raw_due:
        due = datetime.fromisoformat(raw_due)
        due = due.replace(tzinfo=due.tzinfo or UTC).astimezone(UTC)
    else:
        due = datetime.min.replace(tzinfo=UTC)
    return (
        due,
        hashlib.sha256(target.target_identity.encode()).hexdigest(),
        target.target_identity,
    )


def history_aware_registry(
    registry: ProductionSourceRegistry,
    *,
    cohort: int,
    limits: dict[str, int],
    selection_state: dict[str, dict[str, str | None]],
) -> ProductionSourceRegistry:
    """Select the oldest-due targets without resetting progress on registry growth."""

    if cohort < 0:
        raise ValueError("cohort must be non-negative")
    providers = tuple(
        source for source in PROVIDERS if source in registry.target_counts_by_source
    )
    if set(limits) != set(providers) or any(value < 1 for value in limits.values()):
        raise ValueError("refresh limits must contain positive counts for every provider")
    approved = {target.target_identity for target in registry.targets}
    if not approved.issubset(selection_state):
        raise ValueError("selection state is missing an approved target")

    selected = []
    for source in providers:
        candidates = sorted(
            (target for target in registry.targets if target.source == source),
            key=lambda target: _selection_due_key(target, selection_state),
        )
        limit = limits[source]
        if limit > len(candidates):
            raise ValueError(f"refresh limit exceeds {source} registry size")
        selected.extend(candidates[:limit])

    selected.sort(key=lambda target: target.target_identity)
    parent_sha = sha256_json(registry.model_dump(mode="json"))
    identity = sha256_json(
        {
            "parent_registry_sha256": parent_sha,
            "cohort": cohort,
            "limits": limits,
            "selection_strategy": "oldest-due-v1",
            "targets": [target.target_identity for target in selected],
        }
    )[:16]
    return ProductionSourceRegistry(
        registry_id=f"{registry.registry_id}-refresh-{cohort}-{identity}",
        target_universe_git_blob_sha=registry.target_universe_git_blob_sha,
        health_manifest_sha256=registry.health_manifest_sha256,
        health_evidence_updated_at=registry.health_evidence_updated_at,
        approval_policy=registry.approval_policy,
        target_counts_by_source=dict(
            sorted(Counter(target.source for target in selected).items())
        ),
        targets=selected,
    )


def yield_aware_registry(
    registry: ProductionSourceRegistry,
    *,
    cohort: int,
    fairness_limits: dict[str, int],
    selection_state: dict[str, dict[str, str | None]],
    yield_state: dict[str, TargetYieldSignal],
    extra_budget: int = YIELD_PRIORITY_EXTRA_BUDGET,
) -> tuple[ProductionSourceRegistry, dict[str, int]]:
    """Preserve the oldest-due fairness floor and spend only spare capacity on yield.

    The fairness selection is identical to oldest-due-v1. High-yield targets are
    added on top, never substituted for due targets, so the stable-registry
    coverage horizon is not weakened by exploitation.
    """

    fairness = history_aware_registry(
        registry,
        cohort=cohort,
        limits=fairness_limits,
        selection_state=selection_state,
    )
    selected_ids = {target.target_identity for target in fairness.targets}
    spare_capacity = max(0, MAX_REFRESH_TARGETS - len(fairness.targets))
    bonus_budget = min(extra_budget, spare_capacity)
    extras = yield_priority_targets(
        registry,
        already_selected=selected_ids,
        yield_state=yield_state,
        budget=bonus_budget,
    )

    selected = [*fairness.targets, *extras]
    selected.sort(key=lambda target: target.target_identity)
    bonus_counts = dict(sorted(Counter(target.source for target in extras).items()))
    parent_sha = sha256_json(registry.model_dump(mode="json"))
    identity = sha256_json(
        {
            "parent_registry_sha256": parent_sha,
            "cohort": cohort,
            "fairness_limits": fairness_limits,
            "extra_budget": bonus_budget,
            "selection_strategy": "yield-aware-oldest-due-v1",
            "yield_priority_targets": [
                target.target_identity for target in extras
            ],
            "targets": [target.target_identity for target in selected],
        }
    )[:16]
    return (
        ProductionSourceRegistry(
            registry_id=f"{registry.registry_id}-refresh-{cohort}-{identity}",
            target_universe_git_blob_sha=registry.target_universe_git_blob_sha,
            health_manifest_sha256=registry.health_manifest_sha256,
            health_evidence_updated_at=registry.health_evidence_updated_at,
            approval_policy=registry.approval_policy,
            target_counts_by_source=dict(
                sorted(Counter(target.source for target in selected).items())
            ),
            targets=selected,
        ),
        bonus_counts,
    )


def build_history_coverage_capacity(
    registry: ProductionSourceRegistry,
    *,
    limits: dict[str, int],
    selection_strategy: str = "oldest-due-v1",
    selected_counts: dict[str, int] | None = None,
) -> dict[str, object]:
    providers = []
    for source in PROVIDERS:
        count = registry.target_counts_by_source.get(source)
        if count is None:
            continue
        limit = limits[source]
        stable_full_coverage = math.ceil(count / limit)
        providers.append(
            {
                "source": source,
                "registry_targets": count,
                "fairness_targets_per_cohort": limit,
                "selected_targets_per_cohort": (
                    (selected_counts or limits).get(source, limit)
                ),
                "stable_registry_full_coverage_cohorts": stable_full_coverage,
                "fast_provider_horizon_met": (
                    None
                    if source == "workday"
                    else stable_full_coverage <= FAST_PROVIDER_COVERAGE_COHORTS
                ),
            }
        )
    return {
        "selection_strategy": selection_strategy,
        "registry_id": registry.registry_id,
        "registry_sha256": sha256_json(registry.model_dump(mode="json")),
        "growth_semantics": (
            "oldest-due fairness slots are preserved exactly; yield-priority targets "
            "may be added only from spare production capacity, so new admissions "
            "and exploitation cannot reset or displace due targets"
            if selection_strategy == "yield-aware-oldest-due-v1"
            else (
                "targets are ordered by durable due timestamp; new admissions do not "
                "recompute or reset existing targets' progress"
            )
        ),
        "providers": providers,
    }


def build_fan_in_coverage_payload(
    *,
    repository,
    run_id: str,
    parent_registry: ProductionSourceRegistry,
    selected_registry: ProductionSourceRegistry,
    generated_at: datetime,
    fairness_limits: dict[str, int] | None = None,
    selection_strategy: str = "oldest-due-v1",
) -> dict[str, object]:
    """Build the JSON-ready coverage payload after a successful fan-in."""

    theoretical = build_history_coverage_capacity(
        parent_registry,
        limits=fairness_limits or selected_registry.target_counts_by_source,
        selection_strategy=selection_strategy,
        selected_counts=selected_registry.target_counts_by_source,
    )
    observed = build_observed_coverage_report(
        repository=repository,
        run_id=run_id,
        parent_registry=parent_registry,
        selected_registry=selected_registry,
        generated_at=generated_at,
    )
    return {
        "theoretical": theoretical,
        "observed": observed.model_dump(mode="json"),
    }


def _stable(values):
    return sorted(
        values,
        key=lambda target: (
            hashlib.sha256(target.target_identity.encode()).hexdigest(),
            target.target_identity,
        ),
    )


def _refresh_limits(
    registry: ProductionSourceRegistry,
    limits: dict[str, int] | None,
    *,
    cohort: int,
) -> dict[str, int]:
    if limits is not None:
        values = dict(limits)
    else:
        values = default_refresh_limits(registry)
    if sum(values.values()) > MAX_REFRESH_TARGETS:
        raise ValueError(f"refresh cohort exceeds {MAX_REFRESH_TARGETS}-target safety ceiling")
    return values


def rotating_registry(
    registry: ProductionSourceRegistry,
    *,
    cohort: int,
    limits: dict[str, int] | None = None,
) -> ProductionSourceRegistry:
    """Return one deterministic rotating cohort without weakening source approval."""
    if cohort < 0:
        raise ValueError("cohort must be non-negative")
    providers = tuple(source for source in PROVIDERS if source in registry.target_counts_by_source)
    limits = _refresh_limits(registry, limits, cohort=cohort)
    if set(limits) != set(providers) or any(value < 1 for value in limits.values()):
        raise ValueError("refresh limits must contain positive counts for every provider")

    selected = []
    for source in providers:
        candidates = _stable(target for target in registry.targets if target.source == source)
        limit = limits[source]
        if limit > len(candidates):
            raise ValueError(f"refresh limit exceeds {source} registry size")
        start = (cohort * limit) % len(candidates)
        chosen = [candidates[(start + offset) % len(candidates)] for offset in range(limit)]
        selected.extend(chosen)

    selected.sort(key=lambda target: target.target_identity)
    parent_sha = sha256_json(registry.model_dump(mode="json"))
    identity = sha256_json(
        {
            "parent_registry_sha256": parent_sha,
            "cohort": cohort,
            "limits": limits,
            "targets": [target.target_identity for target in selected],
        }
    )[:16]
    return ProductionSourceRegistry(
        registry_id=f"{registry.registry_id}-refresh-{cohort}-{identity}",
        target_universe_git_blob_sha=registry.target_universe_git_blob_sha,
        health_manifest_sha256=registry.health_manifest_sha256,
        health_evidence_updated_at=registry.health_evidence_updated_at,
        approval_policy=registry.approval_policy,
        target_counts_by_source=dict(
            sorted(Counter(target.source for target in selected).items())
        ),
        targets=selected,
    )



def validate_parent_registry_snapshot(
    *,
    parent_registry: ProductionSourceRegistry,
    refresh_plan: RefreshPlan | None,
) -> None:
    if refresh_plan is None:
        return
    parent_registry_sha = sha256_json(parent_registry.model_dump(mode="json"))
    if (
        refresh_plan.parent_registry_id != parent_registry.registry_id
        or refresh_plan.parent_registry_sha256 != parent_registry_sha
    ):
        raise ValueError("fan-in parent registry does not match refresh-plan snapshot")

def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def complete_scheduled_cohort(
    *,
    repository: SQLiteRepository,
    cohort: int,
    fan_in_report: Path,
) -> dict[str, object]:
    fan_in_payload = json.loads(fan_in_report.read_text(encoding="utf-8"))
    fan_in = fan_in_payload.get("fan_in")
    fan_in_status = fan_in.get("status") if isinstance(fan_in, dict) else None
    if fan_in_status != "success":
        return {
            "cohort": cohort,
            "marked_completed": False,
            "fan_in_status": fan_in_status,
            "reason": "fan-in is not complete; logical cohort remains due for retry",
        }
    store = InventoryRefreshScheduleStore(repository)
    changed = store.mark_completed(cohort=cohort)
    return {
        "cohort": cohort,
        "marked_completed": changed,
        "fan_in_status": fan_in_status,
        "last_completed_cohort": store.next_due().last_completed_cohort,
    }


def build_refresh_plan(
    *,
    registry: ProductionSourceRegistry,
    cohort: int,
    limits: dict[str, int] | None = None,
    shards: dict[str, int] | None = None,
    workday_detail_concurrency: int = DEFAULT_WORKDAY_DETAIL_CONCURRENCY,
    workday_briefs: list[WorkdayBriefBinding] | None = None,
    workday_retained_candidates: list[WorkdayRetainedCandidateBinding] | None = None,
    incremental_target_state: list[IncrementalTargetState] | None = None,
    selection_state: dict[str, dict[str, str | None]] | None = None,
    yield_state: dict[str, TargetYieldSignal] | None = None,
    yield_extra_budget: int = YIELD_PRIORITY_EXTRA_BUDGET,
):
    providers = tuple(source for source in PROVIDERS if source in registry.target_counts_by_source)
    fairness_limits = _refresh_limits(registry, limits, cohort=cohort)

    yield_priority_counts: dict[str, int] = {}
    if selection_state is None:
        selection_strategy = "cohort-rotation-v1"
        subset = rotating_registry(
            registry,
            cohort=cohort,
            limits=fairness_limits,
        )
    elif yield_state is None:
        selection_strategy = "oldest-due-v1"
        subset = history_aware_registry(
            registry,
            cohort=cohort,
            limits=fairness_limits,
            selection_state=selection_state,
        )
    else:
        selection_strategy = "yield-aware-oldest-due-v1"
        subset, yield_priority_counts = yield_aware_registry(
            registry,
            cohort=cohort,
            fairness_limits=fairness_limits,
            selection_state=selection_state,
            yield_state=yield_state,
            extra_budget=yield_extra_budget,
        )

    effective_limits = dict(subset.target_counts_by_source)
    if shards is None:
        shards = {
            source: min(
                effective_limits[source],
                max(
                    DEFAULT_SHARDS[source],
                    math.ceil(
                        effective_limits[source] / MAX_TARGETS_PER_SHARD[source]
                    ),
                ),
            )
            for source in providers
        }
    else:
        shards = dict(shards)
    if (
        isinstance(workday_detail_concurrency, bool)
        or not isinstance(workday_detail_concurrency, int)
        or not 1 <= workday_detail_concurrency <= 8
    ):
        raise ValueError("Workday detail concurrency must be an integer from 1 to 8")
    if set(shards) != set(providers) or any(value < 1 for value in shards.values()):
        raise ValueError("refresh shards must contain positive counts for every provider")
    for source in providers:
        if shards[source] > subset.target_counts_by_source[source]:
            raise ValueError(f"shard count exceeds selected {source} targets")
        if math.ceil(subset.target_counts_by_source[source] / shards[source]) > (
            MAX_TARGETS_PER_SHARD[source]
        ):
            raise ValueError(f"{source} shard capacity exceeds production bound")
    if sum(shards.values()) > MAX_REFRESH_SHARDS:
        raise ValueError("refresh shard count exceeds production matrix ceiling")
    manifest = build_shard_manifest(subset, shard_counts_by_source=shards)

    planned_retained_candidates = None
    if workday_retained_candidates is not None:
        all_workday_boards = {
            target.source_target().board_id
            for target in registry.targets
            if target.source == "workday"
        }
        selected_workday_boards = {
            target.source_target().board_id
            for target in subset.targets
            if target.source == "workday"
        }
        seen_boards: set[str] = set()
        planned_retained_candidates = []
        for binding in workday_retained_candidates:
            if binding.board_id not in all_workday_boards:
                raise ValueError("retained Workday candidate board is outside production registry")
            if binding.board_id in seen_boards:
                raise ValueError("duplicate retained Workday candidate board")
            seen_boards.add(binding.board_id)
            if binding.board_id in selected_workday_boards:
                planned_retained_candidates.append(binding)

    planned_incremental_state = None
    if incremental_target_state is not None:
        registry_keys = {
            (target.source, target.source_target().board_id.strip())
            for target in registry.targets
            if target.source in INCREMENTAL_SOURCES
        }
        selected_keys = {
            (target.source, target.source_target().board_id.strip())
            for target in subset.targets
            if target.source in INCREMENTAL_SOURCES
        }
        states_by_key: dict[tuple[str, str], IncrementalTargetState] = {}
        for state in incremental_target_state:
            key = (state.source, state.board_id)
            if key not in registry_keys:
                raise ValueError("incremental target state is outside production registry")
            if key in states_by_key:
                raise ValueError("duplicate incremental target state")
            states_by_key[key] = state
        missing = selected_keys.difference(states_by_key)
        if missing:
            raise ValueError("incremental target state is missing a selected target")
        planned_incremental_state = [
            states_by_key[key] for key in sorted(selected_keys)
        ]

    matrix = {
        "include": [
            {"shard_id": shard.shard_id, "source": shard.source}
            for shard in manifest.shards
        ]
    }
    plan = RefreshPlan(
        parent_registry_id=registry.registry_id,
        parent_registry_sha256=sha256_json(registry.model_dump(mode="json")),
        refresh_registry_id=subset.registry_id,
        refresh_registry_sha256=sha256_json(subset.model_dump(mode="json")),
        shard_manifest_sha256=manifest.manifest_sha256,
        target_limits_by_source=effective_limits,
        shard_counts_by_source=shards,
        target_counts_by_source=subset.target_counts_by_source,
        total_targets=len(subset.targets),
        total_shards=len(manifest.shards),
        selection_strategy=selection_strategy,
        fairness_floor_by_source=(
            fairness_limits if selection_state is not None else None
        ),
        yield_priority_targets_by_source=yield_priority_counts,
        yield_signal_sha256=(
            sha256_json(
                {
                    target_identity: signal.model_dump(mode="json")
                    for target_identity, signal in sorted(yield_state.items())
                }
            )
            if yield_state is not None
            else None
        ),
        workday_detail_concurrency=workday_detail_concurrency,
        workday_briefs=(None if workday_briefs is None else list(workday_briefs)),
        workday_retained_candidates=planned_retained_candidates,
        incremental_target_state=planned_incremental_state,
        matrix=matrix,
    )
    return plan, subset, manifest


def _refresh_collector_factory(
    source: str,
    *,
    workday_detail_concurrency: int,
    registry: ProductionSourceRegistry,
    workday_briefs,
    workday_retained_candidates,
    incremental_target_state,
):
    if source == "workday":
        if workday_briefs is None or workday_retained_candidates is None:
            return WorkdayCollector(detail_concurrency=workday_detail_concurrency)
        return IndexFirstWorkdayCollector(
            registry=registry,
            briefs=workday_briefs,
            detail_concurrency=workday_detail_concurrency,
        )
    if incremental_target_state is not None and source == "greenhouse":
        return GreenhouseCollector(incremental_states=incremental_target_state)
    if incremental_target_state is not None and source == "smartrecruiters":
        return SmartRecruitersCollector(incremental_states=incremental_target_state)
    return default_collector_factory(source)


def collect_refresh_shard(
    *,
    registry: ProductionSourceRegistry,
    manifest: CollectionShardManifest,
    plan: RefreshPlan,
    shard_id: str,
    repo_root: Path = Path("."),
) -> ShardCollectionArtifact:
    """Collect one production refresh shard after validating refresh provenance."""
    registry_sha = sha256_json(registry.model_dump(mode="json"))
    if (
        plan.refresh_registry_id != registry.registry_id
        or plan.refresh_registry_sha256 != registry_sha
        or plan.shard_manifest_sha256 != manifest.manifest_sha256
    ):
        raise ValueError("refresh plan does not match registry/manifest")
    workday_briefs = (
        None
        if plan.workday_briefs is None
        else load_bound_workday_briefs(
            plan.workday_briefs,
            repo_root=repo_root,
        )
    )
    return collect_shard(
        registry=registry,
        manifest=manifest,
        shard_id=shard_id,
        collector_factory=partial(
            _refresh_collector_factory,
            workday_detail_concurrency=plan.workday_detail_concurrency,
            registry=registry,
            workday_briefs=workday_briefs,
            workday_retained_candidates=plan.workday_retained_candidates,
            incremental_target_state=plan.incremental_target_state,
        ),
    )


def _retained_workday_invalidation_identities(
    plan: RefreshPlan,
) -> list[tuple[str, str, str]]:
    if plan.workday_retained_candidates is None:
        return []
    return sorted(
        {
            ("workday", binding.board_id, source_job_id)
            for binding in plan.workday_retained_candidates
            for source_job_id in binding.source_job_ids
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m job_scout.inventory_refresh")
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan")
    plan.add_argument("--registry", type=Path, required=True)
    plan.add_argument("--cohort", type=int, required=True)
    plan.add_argument("--output-dir", type=Path, required=True)
    plan.add_argument("--database", type=Path, required=True)
    plan.add_argument("--plan-dir", type=Path, default=Path("config/sourcing_plans"))
    plan.add_argument("--repo-root", type=Path, default=Path("."))
    plan.add_argument(
        "--workday-limit",
        type=int,
        choices=WORKDAY_RAMP_LEVELS,
        default=DEFAULT_LIMITS["workday"],
        help="Explicit guarded Workday ramp level; never auto-escalates.",
    )
    plan.add_argument(
        "--workday-detail-concurrency",
        type=int,
        choices=range(1, 9),
        default=DEFAULT_WORKDAY_DETAIL_CONCURRENCY,
        help="Bounded concurrent Workday detail reads per target.",
    )

    collect = commands.add_parser("collect")
    collect.add_argument("--registry", type=Path, required=True)
    collect.add_argument("--manifest", type=Path, required=True)
    collect.add_argument("--refresh-plan", type=Path, required=True)
    collect.add_argument("--shard-id", required=True)
    collect.add_argument("--output", type=Path, required=True)
    collect.add_argument("--repo-root", type=Path, default=Path("."))

    fan_in = commands.add_parser("fan-in")
    fan_in.add_argument("--registry", type=Path, required=True)
    fan_in.add_argument("--parent-registry", type=Path, required=True)
    fan_in.add_argument("--manifest", type=Path, required=True)
    fan_in.add_argument("--refresh-plan", type=Path)
    fan_in.add_argument("--artifacts-dir", type=Path, required=True)
    fan_in.add_argument("--database", type=Path, required=True)
    fan_in.add_argument("--output", type=Path, required=True)
    fan_in.add_argument("--retention-hours", type=int, default=72)
    fan_in.add_argument("--skip-retention", action="store_true")

    verify_profiles = commands.add_parser("verify-profile-snapshot")
    verify_profiles.add_argument("--refresh-plan", type=Path, required=True)
    verify_profiles.add_argument("--database", type=Path, required=True)
    verify_profiles.add_argument(
        "--plan-dir", type=Path, default=Path("config/sourcing_plans")
    )
    verify_profiles.add_argument("--repo-root", type=Path, default=Path("."))

    prune = commands.add_parser("prune")
    prune.add_argument("--database", type=Path, required=True)
    prune.add_argument("--output", type=Path, required=True)
    prune.add_argument("--retention-hours", type=int, default=72)

    schedule_next = commands.add_parser("schedule-next")
    schedule_next.add_argument("--database", type=Path, required=True)
    schedule_next.add_argument("--output", type=Path, required=True)

    schedule_complete = commands.add_parser("schedule-complete")
    schedule_complete.add_argument("--database", type=Path, required=True)
    schedule_complete.add_argument("--cohort", type=int, required=True)
    schedule_complete.add_argument("--fan-in-report", type=Path, required=True)
    schedule_complete.add_argument("--output", type=Path, required=True)

    args = parser.parse_args()
    try:
        if args.command == "schedule-next":
            args.database.parent.mkdir(parents=True, exist_ok=True)
            repository = create_repository(args.database)
            decision = InventoryRefreshScheduleStore(repository).next_due()
            payload = {
                "should_run": decision.should_run,
                "cohort": decision.cohort,
                "current_cohort": decision.current_cohort,
                "last_completed_cohort": decision.last_completed_cohort,
            }
            _write_json(args.output, payload)
            print(json.dumps(payload, sort_keys=True))
            return

        if args.command == "schedule-complete":
            args.database.parent.mkdir(parents=True, exist_ok=True)
            repository = create_repository(args.database)
            payload = complete_scheduled_cohort(
                repository=repository,
                cohort=args.cohort,
                fan_in_report=args.fan_in_report,
            )
            _write_json(args.output, payload)
            print(json.dumps(payload, sort_keys=True))
            return

        if args.command == "plan":
            base_registry = load_production_registry(args.registry)
            args.database.parent.mkdir(parents=True, exist_ok=True)
            planning_repository = create_repository(args.database)
            discovery_store = SourceDiscoveryStore(planning_repository)
            admitted_targets, admission_health_evidence = (
                discovery_store.admitted_snapshot(
                    now=datetime.now(UTC),
                    max_health_age_hours=48,
                )
            )
            registry, admission_overlay = overlay_admitted_targets(
                base_registry,
                admitted_targets,
            )
            admission_overlay["health_evidence"] = admission_health_evidence
            admission_overlay["health_evidence_sha256"] = sha256_json(
                admission_health_evidence
            )
            limits = default_refresh_limits(
                registry,
                workday_limit=args.workday_limit,
            )
            workday_briefs = resolve_active_workday_brief_bindings(
                repository=planning_repository,
                plan_dir=args.plan_dir,
                repo_root=args.repo_root,
            )
            loaded_workday_briefs = load_bound_workday_briefs(
                workday_briefs,
                repo_root=args.repo_root,
            )
            workday_retained_candidates = resolve_retained_workday_candidate_bindings(
                repository=planning_repository,
                briefs=loaded_workday_briefs,
            )
            incremental_target_state = snapshot_incremental_target_state(
                planning_repository,
                registry,
            )
            selection_state = load_target_selection_state(
                planning_repository,
                registry,
            )
            yield_state = load_target_yield_state(
                planning_repository,
                registry,
                loaded_workday_briefs,
            )
            refresh, subset, manifest = build_refresh_plan(
                registry=registry,
                cohort=args.cohort,
                limits=limits,
                workday_detail_concurrency=args.workday_detail_concurrency,
                workday_briefs=workday_briefs,
                workday_retained_candidates=workday_retained_candidates,
                incremental_target_state=incremental_target_state,
                selection_state=selection_state,
                yield_state=yield_state,
            )
            _write_json(args.output_dir / "parent-registry.json", registry)
            _write_json(args.output_dir / "registry.json", subset)
            _write_json(args.output_dir / "manifest.json", manifest)
            _write_json(args.output_dir / "admission.json", admission_overlay)
            _write_json(args.output_dir / "matrix.json", refresh.matrix)
            _write_json(args.output_dir / "plan.json", refresh)
            _write_json(
                args.output_dir / "coverage.json",
                build_history_coverage_capacity(
                    registry,
                    limits=refresh.fairness_floor_by_source or limits,
                    selection_strategy=refresh.selection_strategy,
                    selected_counts=refresh.target_counts_by_source,
                ),
            )
            _write_json(
                args.output_dir / "refresh.json",
                {
                    "cohort": args.cohort,
                    "parent_registry_id": registry.registry_id,
                    "parent_registry_sha256": sha256_json(
                        registry.model_dump(mode="json")
                    ),
                    "selected_registry_id": subset.registry_id,
                    "selected_targets": len(subset.targets),
                    "selection_strategy": refresh.selection_strategy,
                    "fairness_floor_by_source": refresh.fairness_floor_by_source,
                    "yield_priority_targets_by_source": (
                        refresh.yield_priority_targets_by_source
                    ),
                    "yield_signal_sha256": refresh.yield_signal_sha256,
                    "yield_positive_targets": sum(
                        signal.eligible_fresh_jobs > 0
                        for signal in yield_state.values()
                    ),
                    "target_counts_by_source": subset.target_counts_by_source,
                    "shard_counts_by_source": refresh.shard_counts_by_source,
                    "active_workday_briefs": len(refresh.workday_briefs),
                    "retained_workday_candidate_ids": sum(
                        len(binding.source_job_ids)
                        for binding in (refresh.workday_retained_candidates or [])
                    ),
                    "incremental_state_targets": len(
                        refresh.incremental_target_state or []
                    ),
                    "incremental_known_provider_ids": sum(
                        len(state.known_source_job_ids)
                        for state in (refresh.incremental_target_state or [])
                    ),
                    "dynamic_admitted_targets": admission_overlay[
                        "dynamic_admitted_targets"
                    ],
                    "dynamic_admission_sha256": admission_overlay[
                        "dynamic_admission_sha256"
                    ],
                },
            )
            print(json.dumps(refresh.model_dump(mode="json"), sort_keys=True))
            return

        if args.command == "verify-profile-snapshot":
            refresh_plan = RefreshPlan.model_validate_json(
                args.refresh_plan.read_text(encoding="utf-8")
            )
            if refresh_plan.workday_briefs is None:
                raise ValueError(
                    "refresh plan predates active Workday SearchBrief snapshot binding"
                )
            args.database.parent.mkdir(parents=True, exist_ok=True)
            repository = create_repository(args.database)
            verify_active_workday_brief_bindings(
                repository=repository,
                plan_dir=args.plan_dir,
                repo_root=args.repo_root,
                expected=refresh_plan.workday_briefs,
            )
            payload = {
                "active_workday_briefs": len(refresh_plan.workday_briefs),
                "snapshot_unchanged": True,
            }
            print(json.dumps(payload, sort_keys=True))
            return

        if args.command == "prune":
            args.database.parent.mkdir(parents=True, exist_ok=True)
            repository = create_repository(args.database)
            retention_started = time.perf_counter()
            retention = repository.prune_stale_inventory(
                retention_hours=args.retention_hours
            )
            payload = {
                "retention": retention,
                "retention_seconds": round(
                    time.perf_counter() - retention_started, 3
                ),
            }
            _write_json(args.output, payload)
            print(json.dumps(payload, sort_keys=True))
            return

        registry = load_production_registry(args.registry)
        manifest = CollectionShardManifest.model_validate_json(
            args.manifest.read_text(encoding="utf-8")
        )
        if args.command == "collect":
            refresh_plan = RefreshPlan.model_validate_json(
                args.refresh_plan.read_text(encoding="utf-8")
            )
            artifact = collect_refresh_shard(
                registry=registry,
                manifest=manifest,
                plan=refresh_plan,
                shard_id=args.shard_id,
                repo_root=args.repo_root,
            )
            _write_json(args.output, artifact)
            print(json.dumps(artifact.model_dump(mode="json"), sort_keys=True))
            return

        refresh_plan: RefreshPlan | None = None
        if args.refresh_plan is not None:
            refresh_plan = RefreshPlan.model_validate_json(
                args.refresh_plan.read_text(encoding="utf-8")
            )
        parent_registry = load_production_registry(args.parent_registry)
        validate_parent_registry_snapshot(
            parent_registry=parent_registry,
            refresh_plan=refresh_plan,
        )

        artifact_paths = sorted(args.artifacts_dir.glob("*.json"))
        if not artifact_paths:
            raise ValueError("no shard artifact files found")
        artifact_load_started = time.perf_counter()
        artifacts = [
            ShardCollectionArtifact.model_validate_json(path.read_text(encoding="utf-8"))
            for path in artifact_paths
        ]
        index_first_used = any(
            target.telemetry.get("collection_mode") == "index_first"
            for artifact in artifacts
            for target in artifact.targets
        )
        invalidated_identities: list[tuple[str, str, str]] = []
        if refresh_plan is not None:
            registry_sha = sha256_json(registry.model_dump(mode="json"))
            if (
                refresh_plan.refresh_registry_id != registry.registry_id
                or refresh_plan.refresh_registry_sha256 != registry_sha
                or refresh_plan.shard_manifest_sha256 != manifest.manifest_sha256
            ):
                raise ValueError("fan-in refresh plan does not match registry/manifest")
            if index_first_used and refresh_plan.workday_retained_candidates is None:
                raise ValueError(
                    "index-first fan-in requires retained Workday candidate snapshot"
                )
            invalidated_identities = _retained_workday_invalidation_identities(
                refresh_plan
            )
        elif index_first_used:
            raise ValueError("index-first fan-in requires --refresh-plan")

        print(
            json.dumps(
                {
                    "event": "inventory_refresh_artifacts_loaded",
                    "shards": len(artifacts),
                    "elapsed_ms": round(
                        (time.perf_counter() - artifact_load_started) * 1000
                    ),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        args.database.parent.mkdir(parents=True, exist_ok=True)
        repository_started = time.perf_counter()
        repository = create_repository(args.database)
        print(
            json.dumps(
                {
                    "event": "inventory_refresh_repository_ready",
                    "elapsed_ms": round((time.perf_counter() - repository_started) * 1000),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        fan_in_started = time.perf_counter()
        report = persist_shard_artifacts(
            repository=repository,
            registry=registry,
            manifest=manifest,
            artifacts=artifacts,
            payload_retention_hours=args.retention_hours,
            invalidated_identities=invalidated_identities,
        )
        fan_in_seconds = time.perf_counter() - fan_in_started
        print(
            json.dumps(
                {
                    "event": "inventory_refresh_fan_in_complete",
                    "elapsed_ms": round(fan_in_seconds * 1000),
                    "normalized_jobs": report.metrics.unique_normalized_jobs,
                    "inventory_memberships": report.metrics.inventory_memberships,
                    "invalidated_retained_jobs": len(invalidated_identities),
                    "replayed": report.replayed,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        coverage_payload = build_fan_in_coverage_payload(
            repository=repository,
            run_id=report.run_id,
            parent_registry=parent_registry,
            selected_registry=registry,
            generated_at=report.persisted_at,
            fairness_limits=(
                refresh_plan.fairness_floor_by_source
                if refresh_plan is not None
                else None
            ),
            selection_strategy=(
                refresh_plan.selection_strategy
                if refresh_plan is not None
                else "oldest-due-v1"
            ),
        )
        _write_json(args.output.parent / "coverage.json", coverage_payload)
        retention = {}
        retention_seconds = 0.0
        if not args.skip_retention:
            retention_started = time.perf_counter()
            retention = repository.prune_stale_inventory(
                retention_hours=args.retention_hours
            )
            retention_seconds = time.perf_counter() - retention_started
            print(
                json.dumps(
                    {
                        "event": "inventory_refresh_prune_complete",
                        "elapsed_ms": round(retention_seconds * 1000),
                        **retention,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
        payload = {
            "fan_in": report.model_dump(mode="json"),
            "coverage": coverage_payload,
            "retention": retention,
            "timings": {
                "fan_in_seconds": round(fan_in_seconds, 3),
                "retention_seconds": round(retention_seconds, 3),
            },
        }
        _write_json(args.output, payload)
        print(json.dumps(payload, sort_keys=True))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        parser.error(f"inventory refresh failed: {exc}")


if __name__ == "__main__":
    main()
