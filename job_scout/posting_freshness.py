"""Shared posting-age eligibility semantics for delivery and benchmarks."""

from __future__ import annotations

from datetime import UTC, datetime

from job_scout.domain.models import UnknownEligibilityPolicy


def posting_freshness_disposition(
    *,
    posted_at: datetime | None,
    max_age_hours: int | None,
    unknown_policy: UnknownEligibilityPolicy,
    evaluated_at: datetime | None,
) -> str | None:
    """Return the production suppression reason, or None when age is eligible."""
    if max_age_hours is None:
        return None
    if posted_at is None:
        return None if unknown_policy is UnknownEligibilityPolicy.ALLOW else "posting_age_unknown"
    if evaluated_at is None:
        raise ValueError("posting freshness requires an explicit evaluation timestamp")

    evaluated = evaluated_at.replace(tzinfo=evaluated_at.tzinfo or UTC).astimezone(UTC)
    posted = posted_at.replace(tzinfo=posted_at.tzinfo or UTC).astimezone(UTC)
    age_seconds = (evaluated - posted).total_seconds()
    if age_seconds < -300:
        return "posting_time_invalid"
    if age_seconds > max_age_hours * 3600:
        return "stale_posting"
    return None
