import json
from pathlib import Path

import pytest

from job_scout.domain.models import (
    CandidateProfile,
    Job,
    MatchDecision,
    RemotePolicy,
    RemoteStatus,
    RoleTargets,
    Seniority,
    Skills,
)
from job_scout.matching.matcher import match_job
from job_scout.normalization.core import content_fingerprint
from job_scout.normalization.location import normalize_location
from job_scout.profile import canonicalize_country_input, create_profile_interactively


def _answers(*values: str):
    iterator = iter(values)
    return lambda prompt: next(iterator)


def _nigeria_profile() -> CandidateProfile:
    return CandidateProfile(
        client_id="taiwo_olumide",
        target_roles=RoleTargets(include=["Support Engineer"]),
        countries={"Nigeria"},
        remote_policy=RemotePolicy(
            allowed={RemoteStatus.REMOTE},
            exclude={RemoteStatus.HYBRID, RemoteStatus.ONSITE},
        ),
        skills=Skills(required=["Troubleshooting"], preferred=["Linux"]),
    )


def _job(*, countries: set[str], description: str = "Linux troubleshooting for customers") -> Job:
    return Job(
        id="job-1",
        source="greenhouse",
        source_job_id="1",
        source_board_id="acme",
        title="Support Engineer",
        company="Acme",
        description_text=description,
        job_url="https://example.com/jobs/1",
        canonical_url="https://example.com/jobs/1",
        eligible_countries=countries,
        remote_status=RemoteStatus.REMOTE,
        content_fingerprint=content_fingerprint(
            title="Support Engineer",
            description=description,
            location="Remote",
            employment_type=None,
        ),
    )


@pytest.mark.parametrize("value", ["Nigeria", "NG", "NGA"])
def test_nigeria_input_is_canonical(value: str) -> None:
    assert canonicalize_country_input(value) == "Nigeria"


def test_interactive_profile_stores_nigeria_structurally(tmp_path) -> None:
    path = create_profile_interactively(
        input_fn=_answers(
            "Taiwo Olumide",
            "Support Engineer",
            "Nigeria",
            "1",
            "Senior, Lead, Staff, Principal, Manager, Director",
            "Troubleshooting",
            "Linux",
            "",
            "",
            "7",
            "Based in Lagos, Nigeria",
            "",
        ),
        output_fn=lambda message: None,
        output_dir=tmp_path,
    )
    assert path is not None
    assert CandidateProfile.model_validate_json(path.read_text()).countries == {"Nigeria"}


def test_corrected_frozen_profile_changes_only_country_constraint() -> None:
    root = Path(__file__).parents[1]
    corrected = json.loads((root / "config/clients/taiwo_olumide_nigeria.json").read_text())
    assert corrected["countries"] == ["Nigeria"]
    assert corrected["client_id"] == "taiwo_olumide"
    assert corrected["target_roles"]["include"] == [
        "IT Support Specialist",
        "Technical Support Engineer",
        "Support Engineer",
        "Help Desk Technician",
        "Service Desk Analyst",
        "Desktop Support Technician",
        "Junior Systems Administrator",
    ]
    assert corrected["skills"]["required"] == ["Troubleshooting"]
    assert set(corrected["excluded_seniority"]) == {
        Seniority.SENIOR.value,
        Seniority.LEAD.value,
        Seniority.STAFF.value,
        Seniority.PRINCIPAL.value,
        Seniority.MANAGER.value,
        Seniority.DIRECTOR.value,
    }


@pytest.mark.parametrize(
    ("location", "country"),
    [
        ("Nigeria", "Nigeria"),
        ("Lagos, Nigeria", "Nigeria"),
        ("India", "India"),
        ("Bangalore, India", "India"),
        ("United Kingdom", "United Kingdom"),
        ("Remote, UK", "United Kingdom"),
        ("United States", "United States"),
        ("Remote, US", "United States"),
        ("Remote, USA", "United States"),
        ("Canada", "Canada"),
    ],
)
def test_explicit_country_evidence_is_normalized(location: str, country: str) -> None:
    normalized = normalize_location(location)
    assert normalized.countries == {country}
    assert normalized.country == country


@pytest.mark.parametrize(
    "location", ["Remote", "Global", "Worldwide", "AMER", "EMEA", "APAC", "Americas"]
)
def test_vague_regions_do_not_fabricate_country(location: str) -> None:
    normalized = normalize_location(location)
    assert normalized.countries == set()
    assert normalized.country is None


def test_explicit_multiple_country_eligibility_is_not_collapsed() -> None:
    normalized = normalize_location("Remote, Canada; Remote, United States")
    assert normalized.countries == {"Canada", "United States"}
    assert normalized.country is None


@pytest.mark.parametrize(
    "countries",
    [
        {"India"},
        {"United Kingdom"},
        {"United States"},
        {"Canada", "United States"},
    ],
)
def test_incompatible_explicit_countries_reject_nigeria_candidate(
    countries: set[str],
) -> None:
    result = match_job(_job(countries=countries), _nigeria_profile())
    assert result.decision is MatchDecision.REJECT
    assert any("do not include the candidate" in reason for reason in result.rejection_reasons)


def test_explicit_nigeria_eligibility_may_continue() -> None:
    assert (
        match_job(_job(countries={"Nigeria"}), _nigeria_profile()).decision
        is MatchDecision.STRONG_MATCH
    )


def test_remote_with_unknown_geography_needs_review() -> None:
    assert (
        match_job(_job(countries=set()), _nigeria_profile()).decision is MatchDecision.NEEDS_REVIEW
    )


def test_remote_status_never_overrides_incompatible_country() -> None:
    job = _job(countries={"United Kingdom"})
    assert job.remote_status is RemoteStatus.REMOTE
    assert match_job(job, _nigeria_profile()).decision is MatchDecision.REJECT


@pytest.mark.parametrize(
    "restriction",
    [
        "You must be a United States Citizen to be eligible.",
        "Applicant must be a U.S. Citizen.",
        "U.S. citizenship required for this role.",
        "You must be authorized to work in the United States.",
    ],
)
def test_explicit_us_work_restrictions_reject_nigeria_candidate(restriction: str) -> None:
    result = match_job(
        _job(countries={"Nigeria"}, description=f"Linux troubleshooting. {restriction}"),
        _nigeria_profile(),
    )
    assert result.decision is MatchDecision.REJECT
    assert any(
        "explicit citizenship or work authorization" in reason
        for reason in result.rejection_reasons
    )
