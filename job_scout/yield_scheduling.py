"""Yield-aware target prioritization for the shared inventory scheduler."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from fractions import Fraction

from pydantic import BaseModel, ConfigDict, Field

from job_scout.domain.models import Job, MatchDecision, SearchBrief
from job_scout.matching.matcher import match_job
from job_scout.production_registry import ProductionSourceRegistry, ProductionTarget
from job_scout.storage.inventory_runs import InventoryRunStore

YIELD_LOOKBACK_HOURS = 72
YIELD_PRIORITY_EXTRA_BUDGET = 100
YIELD_PRIORITY_SOURCES = frozenset(
    {"greenhouse", "ashby", "lever", "smartrecruiters"}
)


class TargetYieldSignal(BaseModel):
    """Rolling client-relevant yield evidence for one approved target."""

    model_config = ConfigDict(extra="forbid")

    target_identity: str
    source: str
    observations: int = Field(ge=0)
    fresh_jobs: int = Field(ge=0)
    eligible_fresh_jobs: int = Field(ge=0)
    eligible_observations: int = Field(ge=0)
    last_eligible_at: datetime | None = None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


def _fresh_24h(job: Job, observed_at: datetime) -> bool:
    if job.posted_at is None:
        return False
    age_seconds = (_utc(observed_at) - _utc(job.posted_at)).total_seconds()
    return -300 <= age_seconds <= 24 * 3600


def load_target_yield_state(
    repository,
    registry: ProductionSourceRegistry,
    briefs: list[SearchBrief],
    *,
    now: datetime | None = None,
    lookback_hours: int = YIELD_LOOKBACK_HOURS,
) -> dict[str, TargetYieldSignal]:
    """Build a bounded rolling yield snapshot from retained production evidence.

    A job contributes client-relevant yield only when it was <=24h old at that
    target observation and deterministically matches at least one currently
    active SearchBrief. The same job is counted once per target across the
    lookback window so frequent polling cannot inflate yield by rediscovering the
    same posting repeatedly.
    """

    if lookback_hours < 1:
        raise ValueError("yield lookback must be at least one hour")

    InventoryRunStore(repository)
    evaluated_at = _utc(now or datetime.now(UTC))
    cutoff = evaluated_at - timedelta(hours=lookback_hours)
    approved = {target.target_identity: target.source for target in registry.targets}

    observations: dict[str, set[str]] = defaultdict(set)
    fresh_ids: dict[str, set[str]] = defaultdict(set)
    eligible_ids: dict[str, set[str]] = defaultdict(set)
    eligible_runs: dict[str, set[str]] = defaultdict(set)
    last_eligible: dict[str, datetime] = {}

    with repository.connect() as connection:
        observation_rows = connection.execute(
            "SELECT run_id,target_identity,source,completed_at "
            "FROM inventory_target_observations "
            "WHERE status IN ('success','partial') AND completed_at>=? "
            "ORDER BY completed_at,target_identity",
            (cutoff.isoformat(),),
        ).fetchall()
        membership_rows = connection.execute(
            "SELECT o.run_id,o.target_identity,o.source,o.completed_at,"
            "r.job_id,j.payload_json "
            "FROM inventory_target_observations o "
            "JOIN inventory_run_jobs r "
            "ON r.run_id=o.run_id AND r.target_identity=o.target_identity "
            "JOIN jobs j ON j.id=r.job_id "
            "WHERE o.status IN ('success','partial') AND o.completed_at>=? "
            "ORDER BY o.completed_at,o.target_identity,r.job_id",
            (cutoff.isoformat(),),
        ).fetchall()

    for row in observation_rows:
        target_identity = row["target_identity"]
        if approved.get(target_identity) == row["source"]:
            observations[target_identity].add(row["run_id"])

    for row in membership_rows:
        target_identity = row["target_identity"]
        if approved.get(target_identity) != row["source"]:
            continue
        job = Job.model_validate_json(row["payload_json"])
        observed_at = datetime.fromisoformat(row["completed_at"])
        if not _fresh_24h(job, observed_at):
            continue
        fresh_ids[target_identity].add(job.id)
        if not briefs:
            continue
        eligible = any(
            match_job(job, brief).decision
            in {MatchDecision.STRONG_MATCH, MatchDecision.POSSIBLE_MATCH}
            for brief in briefs
        )
        if not eligible or job.id in eligible_ids[target_identity]:
            continue
        eligible_ids[target_identity].add(job.id)
        eligible_runs[target_identity].add(row["run_id"])
        current = last_eligible.get(target_identity)
        normalized_observed_at = _utc(observed_at)
        if current is None or normalized_observed_at > current:
            last_eligible[target_identity] = normalized_observed_at

    return {
        target_identity: TargetYieldSignal(
            target_identity=target_identity,
            source=source,
            observations=len(observations[target_identity]),
            fresh_jobs=len(fresh_ids[target_identity]),
            eligible_fresh_jobs=len(eligible_ids[target_identity]),
            eligible_observations=len(eligible_runs[target_identity]),
            last_eligible_at=last_eligible.get(target_identity),
        )
        for target_identity, source in sorted(approved.items())
    }


def _rank_key(
    target: ProductionTarget,
    signal: TargetYieldSignal,
) -> tuple[Fraction, int, int, int, float, str, str]:
    observations = max(1, signal.observations)
    rate = Fraction(signal.eligible_fresh_jobs, observations)
    last_eligible = (
        signal.last_eligible_at.timestamp() if signal.last_eligible_at is not None else 0.0
    )
    return (
        -rate,
        -signal.eligible_fresh_jobs,
        -signal.eligible_observations,
        -signal.fresh_jobs,
        -last_eligible,
        hashlib.sha256(target.target_identity.encode()).hexdigest(),
        target.target_identity,
    )


def yield_priority_targets(
    registry: ProductionSourceRegistry,
    *,
    already_selected: set[str],
    yield_state: dict[str, TargetYieldSignal],
    budget: int,
) -> list[ProductionTarget]:
    """Return extra high-yield targets without consuming fairness-floor slots."""

    if budget < 0:
        raise ValueError("yield priority budget must not be negative")
    if budget == 0:
        return []

    candidates: list[tuple[ProductionTarget, TargetYieldSignal]] = []
    for target in registry.targets:
        if target.target_identity in already_selected:
            continue
        if target.source not in YIELD_PRIORITY_SOURCES:
            continue
        signal = yield_state.get(target.target_identity)
        if signal is None or signal.source != target.source:
            continue
        if signal.eligible_fresh_jobs < 1:
            continue
        candidates.append((target, signal))

    candidates.sort(key=lambda item: _rank_key(item[0], item[1]))
    return [target for target, _signal in candidates[:budget]]
