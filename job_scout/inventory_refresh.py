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
from job_scout.storage.inventory_runs import InventoryRunStore
from job_scout.storage.sqlite import SQLiteRepository
from job_scout.workday_production import (
    IndexFirstWorkdayCollector,
    WorkdayBriefBinding,
    load_bound_workday_briefs,
    resolve_active_workday_brief_bindings,
)

DEFAULT_LIMITS = {
    "greenhouse": 40,
    "ashby": 40,
    "workday": 1,
    "lever": 19,
    "smartrecruiters": 1,
}
DEFAULT_SHARDS = {
    "greenhouse": 6,
    "ashby": 6,
    "workday": 1,
    "lever": 4,
    "smartrecruiters": 1,
}
DEFAULT_WORKDAY_DETAIL_CONCURRENCY = 4
MAX_REFRESH_TARGETS = 125
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
    total_shards: int = Field(ge=1)
    workday_detail_concurrency: int = Field(ge=1, le=8)
    workday_briefs: list[WorkdayBriefBinding] = Field(default_factory=list)
    matrix: dict[str, list[dict[str, str]]]


def default_refresh_limits(
    registry: ProductionSourceRegistry,
    *,
    workday_limit: int | None = None,
) -> dict[str, int]:
    limits = {
        source: DEFAULT_LIMITS[source]
        for source in PROVIDERS
        if source in registry.target_counts_by_source
    }
    if "workday" in limits and workday_limit is not None:
        if workday_limit not in WORKDAY_RAMP_LEVELS:
            raise ValueError(
                f"Workday refresh limit must be one of {WORKDAY_RAMP_LEVELS}"
            )
        limits["workday"] = min(
            workday_limit,
            registry.target_counts_by_source["workday"],
        )
    # SmartRecruiters is additive only after explicit registry admission.
    if "smartrecruiters" in limits and "greenhouse" in limits:
        limits["greenhouse"] -= limits["smartrecruiters"]
    if sum(limits.values()) > MAX_REFRESH_TARGETS:
        raise ValueError("default refresh cohort exceeds safety ceiling")
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


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_refresh_plan(
    *,
    registry: ProductionSourceRegistry,
    cohort: int,
    limits: dict[str, int] | None = None,
    shards: dict[str, int] | None = None,
    workday_detail_concurrency: int = DEFAULT_WORKDAY_DETAIL_CONCURRENCY,
    workday_briefs: list[WorkdayBriefBinding] | None = None,
):
    providers = tuple(source for source in PROVIDERS if source in registry.target_counts_by_source)
    limits = _refresh_limits(registry, limits, cohort=cohort)
    if shards is None:
        shards = {source: DEFAULT_SHARDS[source] for source in providers}
        if "workday" in shards:
            # Workday target runtimes have a very long tail. Isolate every selected
            # Workday company so one slow board cannot serialize four healthy boards
            # behind it. Detail concurrency remains independently capped per target.
            shards["workday"] = limits["workday"]
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

    subset = rotating_registry(registry, cohort=cohort, limits=limits)
    for source in providers:
        if shards[source] > subset.target_counts_by_source[source]:
            raise ValueError(f"shard count exceeds selected {source} targets")
    manifest = build_shard_manifest(subset, shard_counts_by_source=shards)
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
        target_limits_by_source=limits,
        shard_counts_by_source=shards,
        target_counts_by_source=subset.target_counts_by_source,
        total_targets=len(subset.targets),
        total_shards=len(manifest.shards),
        workday_detail_concurrency=workday_detail_concurrency,
        workday_briefs=list(workday_briefs or []),
        matrix=matrix,
    )
    return plan, subset, manifest


def _refresh_collector_factory(
    source: str,
    *,
    workday_detail_concurrency: int,
    registry: ProductionSourceRegistry,
    workday_briefs,
):
    if source == "workday":
        return IndexFirstWorkdayCollector(
            registry=registry,
            briefs=workday_briefs,
            detail_concurrency=workday_detail_concurrency,
        )
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
    workday_briefs = load_bound_workday_briefs(
        plan.workday_briefs,
        repo_root=repo_root,
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
        ),
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

    fan_in = commands.add_parser("fan-in")
    fan_in.add_argument("--registry", type=Path, required=True)
    fan_in.add_argument("--parent-registry", type=Path, required=True)
    fan_in.add_argument("--manifest", type=Path, required=True)
    fan_in.add_argument("--artifacts-dir", type=Path, required=True)
    fan_in.add_argument("--database", type=Path, required=True)
    fan_in.add_argument("--output", type=Path, required=True)
    fan_in.add_argument("--retention-hours", type=int, default=72)
    fan_in.add_argument("--skip-retention", action="store_true")

    prune = commands.add_parser("prune")
    prune.add_argument("--database", type=Path, required=True)
    prune.add_argument("--output", type=Path, required=True)
    prune.add_argument("--retention-hours", type=int, default=72)

    args = parser.parse_args()
    try:
        if args.command == "plan":
            registry = load_production_registry(args.registry)
            limits = default_refresh_limits(
                registry,
                workday_limit=args.workday_limit,
            )
            args.database.parent.mkdir(parents=True, exist_ok=True)
            planning_repository = SQLiteRepository(args.database)
            workday_briefs = resolve_active_workday_brief_bindings(
                repository=planning_repository,
                plan_dir=args.plan_dir,
                repo_root=args.repo_root,
            )
            refresh, subset, manifest = build_refresh_plan(
                registry=registry,
                cohort=args.cohort,
                limits=limits,
                workday_detail_concurrency=args.workday_detail_concurrency,
                workday_briefs=workday_briefs,
            )
            _write_json(args.output_dir / "registry.json", subset)
            _write_json(args.output_dir / "manifest.json", manifest)
            _write_json(args.output_dir / "matrix.json", refresh.matrix)
            _write_json(args.output_dir / "plan.json", refresh)
            _write_json(
                args.output_dir / "coverage.json",
                build_coverage_proof(registry, limits=limits),
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
                    "target_counts_by_source": subset.target_counts_by_source,
                    "active_workday_briefs": len(refresh.workday_briefs),
                },
            )
            print(json.dumps(refresh.model_dump(mode="json"), sort_keys=True))
            return

        if args.command == "prune":
            args.database.parent.mkdir(parents=True, exist_ok=True)
            repository = SQLiteRepository(args.database)
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
            )
            _write_json(args.output, artifact)
            print(json.dumps(artifact.model_dump(mode="json"), sort_keys=True))
            return

        artifact_paths = sorted(args.artifacts_dir.glob("*.json"))
        if not artifact_paths:
            raise ValueError("no shard artifact files found")
        artifact_load_started = time.perf_counter()
        artifacts = [
            ShardCollectionArtifact.model_validate_json(path.read_text(encoding="utf-8"))
            for path in artifact_paths
        ]
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
        repository = SQLiteRepository(args.database)
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
        )
        fan_in_seconds = time.perf_counter() - fan_in_started
        print(
            json.dumps(
                {
                    "event": "inventory_refresh_fan_in_complete",
                    "elapsed_ms": round(fan_in_seconds * 1000),
                    "normalized_jobs": report.metrics.unique_normalized_jobs,
                    "inventory_memberships": report.metrics.inventory_memberships,
                    "replayed": report.replayed,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        parent_registry = load_production_registry(args.parent_registry)
        coverage_proof = build_coverage_proof(
            parent_registry,
            limits=registry.target_counts_by_source,
        )
        observed_coverage = build_observed_coverage_report(
            repository=repository,
            run_id=report.run_id,
            parent_registry=parent_registry,
            selected_registry=registry,
            generated_at=report.persisted_at,
        )
        coverage_payload = {
            "theoretical": coverage_proof.model_dump(mode="json"),
            "observed": observed_coverage.model_dump(mode="json"),
        }
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
