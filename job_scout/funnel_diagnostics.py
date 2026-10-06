"""Aggregate client-match funnel diagnostics without changing matcher semantics."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from job_scout.domain.models import Job, JobMatch, MatchDecision


class FunnelModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MatchAgeBuckets(FunnelModel):
    age_0_24h: int = Field(default=0, ge=0)
    age_24_48h: int = Field(default=0, ge=0)
    age_48_72h: int = Field(default=0, ge=0)
    age_over_72h: int = Field(default=0, ge=0)
    unknown_age: int = Field(default=0, ge=0)
    invalid_time: int = Field(default=0, ge=0)

    @property
    def total(self) -> int:
        return (
            self.age_0_24h
            + self.age_24_48h
            + self.age_48_72h
            + self.age_over_72h
            + self.unknown_age
            + self.invalid_time
        )


class ClientFunnelSlice(FunnelModel):
    retained_evaluated: int = Field(default=0, ge=0)
    retained_confirmed_matches: int = Field(default=0, ge=0)
    retained_needs_review_matches: int = Field(default=0, ge=0)
    retained_rejected: int = Field(default=0, ge=0)

    fresh_0_24h: int = Field(default=0, ge=0)
    title_matched_0_24h: int = Field(default=0, ge=0)
    target_market_survived_0_24h: int = Field(default=0, ge=0)
    remote_survived_0_24h: int = Field(default=0, ge=0)
    other_rules_survived_0_24h: int = Field(default=0, ge=0)
    confirmed_matches_0_24h: int = Field(default=0, ge=0)
    needs_review_matches_0_24h: int = Field(default=0, ge=0)
    rejected_0_24h: int = Field(default=0, ge=0)

    match_age_buckets: MatchAgeBuckets = Field(default_factory=MatchAgeBuckets)

    @model_validator(mode="after")
    def accounting(self) -> ClientFunnelSlice:
        if self.retained_evaluated != (
            self.retained_confirmed_matches
            + self.retained_needs_review_matches
            + self.retained_rejected
        ):
            raise ValueError("retained match decisions must partition evaluated inventory")
        if self.match_age_buckets.total != (
            self.retained_confirmed_matches + self.retained_needs_review_matches
        ):
            raise ValueError("match age buckets must partition retained reviewable supply")
        if self.fresh_0_24h != (
            self.confirmed_matches_0_24h
            + self.needs_review_matches_0_24h
            + self.rejected_0_24h
        ):
            raise ValueError("fresh decisions must partition the <=24h cohort")
        if not (
            self.other_rules_survived_0_24h
            == self.confirmed_matches_0_24h + self.needs_review_matches_0_24h
            <= self.remote_survived_0_24h
            <= self.target_market_survived_0_24h
            <= self.title_matched_0_24h
            <= self.fresh_0_24h
        ):
            raise ValueError("fresh funnel stages must be cumulative and monotonic")
        return self


class ClientFunnelDiagnostics(FunnelModel):
    overall: ClientFunnelSlice
    by_source: dict[str, ClientFunnelSlice]

    @model_validator(mode="after")
    def source_accounting(self) -> ClientFunnelDiagnostics:
        scalar_fields = [
            name
            for name in ClientFunnelSlice.model_fields
            if name != "match_age_buckets"
        ]
        for name in scalar_fields:
            if getattr(self.overall, name) != sum(
                getattr(source, name) for source in self.by_source.values()
            ):
                raise ValueError(f"source funnel accounting does not balance for {name}")
        for name in MatchAgeBuckets.model_fields:
            if getattr(self.overall.match_age_buckets, name) != sum(
                getattr(source.match_age_buckets, name)
                for source in self.by_source.values()
            ):
                raise ValueError(f"source age-bucket accounting does not balance for {name}")
        return self


@dataclass
class _MutableSlice:
    retained_evaluated: int = 0
    retained_confirmed_matches: int = 0
    retained_needs_review_matches: int = 0
    retained_rejected: int = 0

    fresh_0_24h: int = 0
    title_matched_0_24h: int = 0
    target_market_survived_0_24h: int = 0
    remote_survived_0_24h: int = 0
    other_rules_survived_0_24h: int = 0
    confirmed_matches_0_24h: int = 0
    needs_review_matches_0_24h: int = 0
    rejected_0_24h: int = 0

    match_age_buckets: dict[str, int] = field(
        default_factory=lambda: {name: 0 for name in MatchAgeBuckets.model_fields}
    )

    def snapshot(self) -> ClientFunnelSlice:
        return ClientFunnelSlice(
            retained_evaluated=self.retained_evaluated,
            retained_confirmed_matches=self.retained_confirmed_matches,
            retained_needs_review_matches=self.retained_needs_review_matches,
            retained_rejected=self.retained_rejected,
            fresh_0_24h=self.fresh_0_24h,
            title_matched_0_24h=self.title_matched_0_24h,
            target_market_survived_0_24h=self.target_market_survived_0_24h,
            remote_survived_0_24h=self.remote_survived_0_24h,
            other_rules_survived_0_24h=self.other_rules_survived_0_24h,
            confirmed_matches_0_24h=self.confirmed_matches_0_24h,
            needs_review_matches_0_24h=self.needs_review_matches_0_24h,
            rejected_0_24h=self.rejected_0_24h,
            match_age_buckets=MatchAgeBuckets(**self.match_age_buckets),
        )


def posting_age_bucket(posted_at: datetime | None, evaluated_at: datetime) -> str:
    """Return an observability-only posting-age bucket using production clock tolerance."""
    if posted_at is None:
        return "unknown_age"
    evaluated = evaluated_at.replace(
        tzinfo=evaluated_at.tzinfo or UTC
    ).astimezone(UTC)
    posted = posted_at.replace(tzinfo=posted_at.tzinfo or UTC).astimezone(UTC)
    age_seconds = (evaluated - posted).total_seconds()
    if age_seconds < -300:
        return "invalid_time"
    age_seconds = max(0.0, age_seconds)
    if age_seconds <= 24 * 3600:
        return "age_0_24h"
    if age_seconds <= 48 * 3600:
        return "age_24_48h"
    if age_seconds <= 72 * 3600:
        return "age_48_72h"
    return "age_over_72h"


def _has_rejection_prefix(match: JobMatch, prefixes: tuple[str, ...]) -> bool:
    return any(
        reason.casefold().startswith(prefix)
        for reason in match.rejection_reasons
        for prefix in prefixes
    )


def _observe(slice_: _MutableSlice, job: Job, match: JobMatch, evaluated_at: datetime) -> None:
    slice_.retained_evaluated += 1
    if match.decision in {MatchDecision.STRONG_MATCH, MatchDecision.POSSIBLE_MATCH}:
        slice_.retained_confirmed_matches += 1
    elif match.decision is MatchDecision.NEEDS_REVIEW:
        slice_.retained_needs_review_matches += 1
    else:
        slice_.retained_rejected += 1

    age_bucket = posting_age_bucket(job.posted_at, evaluated_at)
    if match.decision is not MatchDecision.REJECT:
        slice_.match_age_buckets[age_bucket] += 1

    if age_bucket != "age_0_24h":
        return

    slice_.fresh_0_24h += 1
    if match.decision in {MatchDecision.STRONG_MATCH, MatchDecision.POSSIBLE_MATCH}:
        slice_.confirmed_matches_0_24h += 1
    elif match.decision is MatchDecision.NEEDS_REVIEW:
        slice_.needs_review_matches_0_24h += 1
    else:
        slice_.rejected_0_24h += 1

    if _has_rejection_prefix(
        match, ("title does not match a configured target role",)
    ):
        return
    slice_.title_matched_0_24h += 1

    if _has_rejection_prefix(match, ("target market ",)):
        return
    slice_.target_market_survived_0_24h += 1

    if _has_rejection_prefix(match, ("work mode ", "remote status ")):
        return
    slice_.remote_survived_0_24h += 1

    if match.decision is MatchDecision.REJECT:
        return
    slice_.other_rules_survived_0_24h += 1


class ClientFunnelAccumulator:
    """Collect aggregate diagnostics while the authoritative matcher already runs."""

    def __init__(self, evaluated_at: datetime) -> None:
        self.evaluated_at = evaluated_at
        self._overall = _MutableSlice()
        self._by_source: dict[str, _MutableSlice] = {}

    def observe(self, job: Job, match: JobMatch) -> None:
        source = job.source.strip().casefold() or "unknown"
        source_slice = self._by_source.setdefault(source, _MutableSlice())
        _observe(self._overall, job, match, self.evaluated_at)
        _observe(source_slice, job, match, self.evaluated_at)

    def snapshot(self) -> ClientFunnelDiagnostics:
        return ClientFunnelDiagnostics(
            overall=self._overall.snapshot(),
            by_source={
                source: self._by_source[source].snapshot()
                for source in sorted(self._by_source)
            },
        )
