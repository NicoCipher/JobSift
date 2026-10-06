from __future__ import annotations

from datetime import UTC, datetime, timedelta

from job_scout.domain.models import (
    Job,
    RemoteStatus,
    RuleIntent,
    SearchBrief,
    TargetMarketRule,
    UnknownEligibilityPolicy,
    WorkModeRule,
)
from job_scout.funnel_diagnostics import ClientFunnelAccumulator
from job_scout.matching.matcher import match_job


NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def posting(
    job_id: str,
    *,
    title: str = "Software Engineer",
    country: str | None = "United States",
    remote_status: RemoteStatus = RemoteStatus.REMOTE,
    posted_at: datetime | None = NOW - timedelta(hours=1),
    source: str = "greenhouse",
) -> Job:
    return Job(
        id=job_id,
        source=source,
        source_job_id=job_id,
        source_board_id="acme",
        title=title,
        company="Acme",
        employer_id="acme",
        description_text="Build and maintain software systems.",
        job_url=f"https://example.com/{job_id}",
        canonical_url=f"https://example.com/{job_id}",
        country=country,
        eligible_countries={country} if country else set(),
        remote_status=remote_status,
        posted_at=posted_at,
        discovered_at=NOW,
        last_seen_at=NOW,
        content_fingerprint=job_id,
    )


def brief() -> SearchBrief:
    return SearchBrief(
        client_id="client-a",
        target_roles=["Software Engineer"],
        target_market=TargetMarketRule(
            countries={"United States"},
            intent=RuleIntent.MUST,
            unknown_policy=UnknownEligibilityPolicy.REVIEW,
        ),
        work_mode=WorkModeRule(
            modes={RemoteStatus.REMOTE},
            intent=RuleIntent.MUST,
            unknown_policy=UnknownEligibilityPolicy.REVIEW,
        ),
        management_roles=RuleIntent.AVOID,
    )


def test_fresh_funnel_is_cumulative_and_match_age_buckets_are_separate() -> None:
    jobs = [
        posting("title-reject", title="Account Executive"),
        posting("market-reject", country="Canada"),
        posting("remote-reject", remote_status=RemoteStatus.ONSITE),
        posting("other-reject", title="Software Engineer Manager"),
        posting(
            "reviewable",
            country=None,
            remote_status=RemoteStatus.UNKNOWN,
        ),
        posting("confirmed"),
        posting(
            "match-30h",
            posted_at=NOW - timedelta(hours=30),
            source="ashby",
        ),
        posting(
            "match-60h",
            posted_at=NOW - timedelta(hours=60),
            source="ashby",
        ),
        posting("match-unknown", posted_at=None, source="ashby"),
        posting(
            "match-invalid-future",
            posted_at=NOW + timedelta(minutes=10),
            source="ashby",
        ),
    ]

    accumulator = ClientFunnelAccumulator(NOW)
    for job in jobs:
        match = match_job(job, brief()).model_copy(update={"evaluated_at": NOW})
        accumulator.observe(job, match)

    report = accumulator.snapshot()
    overall = report.overall

    assert overall.retained_evaluated == 10
    assert overall.retained_confirmed_matches == 5
    assert overall.retained_needs_review_matches == 1
    assert overall.retained_rejected == 4

    assert overall.fresh_0_24h == 6
    assert overall.title_matched_0_24h == 5
    assert overall.target_market_survived_0_24h == 4
    assert overall.remote_survived_0_24h == 3
    assert overall.other_rules_survived_0_24h == 2
    assert overall.confirmed_matches_0_24h == 1
    assert overall.needs_review_matches_0_24h == 1
    assert overall.rejected_0_24h == 4

    assert overall.match_age_buckets.model_dump() == {
        "age_0_24h": 2,
        "age_24_48h": 1,
        "age_48_72h": 1,
        "age_over_72h": 0,
        "unknown_age": 1,
        "invalid_time": 1,
    }
    assert set(report.by_source) == {"ashby", "greenhouse"}
    assert report.by_source["ashby"].retained_confirmed_matches == 4
    assert report.by_source["greenhouse"].fresh_0_24h == 6

    # The cumulative differences are the deterministic primary loss buckets:
    # title, target market, remote, then all remaining SearchBrief rules.
    assert overall.fresh_0_24h - overall.title_matched_0_24h == 1
    assert (
        overall.title_matched_0_24h - overall.target_market_survived_0_24h
        == 1
    )
    assert (
        overall.target_market_survived_0_24h - overall.remote_survived_0_24h
        == 1
    )
    assert (
        overall.remote_survived_0_24h - overall.other_rules_survived_0_24h
        == 1
    )
