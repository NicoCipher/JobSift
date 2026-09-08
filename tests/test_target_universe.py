from __future__ import annotations

import pytest
from pydantic import ValidationError

from job_scout.sourcing_plan import SourcingPlan
from job_scout.target_universe import (
    HistoricalLink,
    TargetRecord,
    build_target_universe,
    derive_link,
)


def link(url: str, *, company: str = "Company", order: int = 0) -> HistoricalLink:
    return HistoricalLink(
        sheet="Sheet 1",
        row=order + 2,
        title="Engineer",
        company=company,
        url=url,
        status="Applied",
        order=order,
    )


def universe(*links: HistoricalLink):
    return build_target_universe(
        links,
        input_provenance={"sha256": "input"},
        generated_at="2026-09-08T00:00:00+00:00",
    )


def test_greenhouse_historical_url_derives_canonical_board() -> None:
    result = derive_link(link("https://job-boards.greenhouse.io/acme/jobs/123?gh_src=source"))

    assert result.identity == "greenhouse:acme"
    assert result.coordinates == {"board": "acme"}


def test_ashby_historical_url_derives_canonical_board() -> None:
    result = derive_link(
        link("https://jobs.ashbyhq.com/acme/12345678-1234-4234-8234-123456789abc/application")
    )

    assert result.identity == "ashby:acme"


def test_lever_global_and_eu_targets_remain_distinct() -> None:
    posting = "12345678-1234-4234-8234-123456789abc"
    global_result = derive_link(link(f"https://jobs.lever.co/acme/{posting}"))
    eu_result = derive_link(link(f"https://jobs.eu.lever.co/acme/{posting}"))

    assert global_result.identity == "lever:global:acme"
    assert eu_result.identity == "lever:eu:acme"


def test_lever_hosted_and_apply_urls_collapse_to_one_target() -> None:
    posting = "12345678-1234-4234-8234-123456789abc"
    result = universe(
        link(f"https://jobs.lever.co/acme/{posting}", order=0),
        link(f"https://jobs.lever.co/acme/{posting}/apply?source=LinkedIn", order=1),
    )

    assert result.target_counts_by_source == {"lever": 1}
    assert result.target_records[0].historical_occurrence_count == 2
    assert result.target_records[0].distinct_historical_url_count == 2


def test_workday_url_derives_exact_coordinates() -> None:
    result = derive_link(
        link("https://acme.wd12.myworkdayjobs.com/en-US/External/job/Remote/Engineer_R123")
    )

    assert result.identity == "workday:acme.wd12.myworkdayjobs.com:acme:External"
    assert result.coordinates == {
        "host": "acme.wd12.myworkdayjobs.com",
        "tenant": "acme",
        "site": "External",
    }


def test_workday_details_route_is_supported() -> None:
    result = derive_link(
        link("https://acme.wd1.myworkdayjobs.com/Careers/jobs/details/Engineer_R123")
    )

    assert result.identity == "workday:acme.wd1.myworkdayjobs.com:acme:Careers"


def test_unresolved_workday_url_does_not_guess_target() -> None:
    result = derive_link(link("https://acme.wd1.myworkdayjobs.com/en-US/1/userHome"))

    assert result.classification == "recognized_source_unresolved_target"
    assert result.identity is None


def test_malformed_and_unsupported_links_are_classified() -> None:
    malformed = derive_link(link("acme.wd1.myworkdayjobs.com/Careers/job/Remote/Engineer_R123"))
    unsupported = derive_link(link("https://ats.example.test/jobs/123"))

    assert malformed.classification == "malformed_url"
    assert unsupported.classification == "unsupported_source"


def test_duplicate_vacancies_collapse_and_occurrences_are_preserved() -> None:
    result = universe(
        link("https://job-boards.greenhouse.io/acme/jobs/1", order=0),
        link("https://job-boards.greenhouse.io/acme/jobs/2", order=1),
        link("https://job-boards.greenhouse.io/acme/jobs/1", order=2),
    )

    record = result.target_records[0]
    assert record.target_identity == "greenhouse:acme"
    assert record.historical_occurrence_count == 3
    assert record.distinct_historical_url_count == 2


def test_company_hint_does_not_change_technical_identity() -> None:
    result = universe(
        link("https://job-boards.greenhouse.io/acme/jobs/1", company="Acme Inc", order=0),
        link("https://job-boards.greenhouse.io/acme/jobs/2", company="Acme", order=1),
    )

    assert [record.target_identity for record in result.target_records] == ["greenhouse:acme"]
    assert result.target_records[0].company_hint == "Acme"


def test_target_ordering_is_deterministic() -> None:
    links = [
        link("https://jobs.lever.co/zulu/12345678-1234-4234-8234-123456789abc", order=1),
        link("https://job-boards.greenhouse.io/beta/jobs/1", order=0),
        link("https://job-boards.greenhouse.io/alpha/jobs/1", order=2),
    ]

    assert [record.target_identity for record in universe(*links).target_records] == [
        "greenhouse:alpha",
        "greenhouse:beta",
        "lever:global:zulu",
    ]


def test_classification_totals_reconcile_full_input() -> None:
    result = universe(
        link("https://job-boards.greenhouse.io/acme/jobs/1"),
        link("https://jobs.ashbyhq.com/acme/not-a-uuid"),
        link("mailto:jobs@example.test"),
        link("https://example.test/jobs/1"),
    )

    assert sum(result.classification_counts.values()) == result.total_historical_rows == 4
    assert result.classification_counts == {
        "malformed_url": 1,
        "recognized_source_unresolved_target": 1,
        "supported_target": 1,
        "unsupported_source": 1,
    }


def test_target_identities_are_accepted_by_sourcing_plan() -> None:
    target_records = universe(
        link("https://job-boards.greenhouse.io/green/jobs/1"),
        link("https://jobs.ashbyhq.com/ash/12345678-1234-4234-8234-123456789abc"),
        link("https://work.wd1.myworkdayjobs.com/External/job/Remote/Engineer_R1"),
        link("https://jobs.eu.lever.co/lever/12345678-1234-4234-8234-123456789abc"),
    ).target_records
    targets = []
    for record in target_records:
        item = {"source": record.source, "company": record.company_hint or "Unknown"}
        item.update(record.coordinates)
        targets.append(item)

    plan = SourcingPlan.model_validate(
        {
            "plan_id": "target-universe-convention",
            "search_brief": "brief.json",
            "database": "jobs.sqlite3",
            "csv": "jobs.csv",
            "targets": targets,
        }
    )
    assert [target.target_identity for target in plan.targets] == [
        record.target_identity for record in target_records
    ]


def test_target_record_rejects_source_coordinate_identity_mismatch() -> None:
    valid = universe(link("https://job-boards.greenhouse.io/acme/jobs/1")).target_records[0]
    payload = valid.model_dump()
    payload["target_identity"] = "greenhouse:other"

    with pytest.raises(ValidationError, match="target identity does not match"):
        TargetRecord.model_validate(payload)
