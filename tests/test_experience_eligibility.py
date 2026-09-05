from pathlib import Path

import pytest

from job_scout.domain.models import CandidateProfile, Job, MatchDecision, RoleTargets, Skills
from job_scout.matching.experience import (
    ExperienceEvidenceKind,
    extract_experience_evidence,
)
from job_scout.matching.matcher import MATCHER_VERSION, match_job
from job_scout.normalization.core import content_fingerprint
from job_scout.profile import create_profile_interactively


def _minimum(text: str) -> int | None:
    return extract_experience_evidence(text).mandatory_minimum_years


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3+ years of experience", 3),
        ("at least 3 years", 3),
        ("minimum 5 years", 5),
        ("5-7 years of experience", 5),
        ("2 or more years", 2),
        ("3 years of professional experience", 3),
        ("7+ years working with PostgreSQL", 7),
    ],
)
def test_explicit_minimum_experience_is_extracted(text: str, expected: int) -> None:
    assert _minimum(text) == expected


def _job(description: str) -> Job:
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
        content_fingerprint=content_fingerprint(
            title="Support Engineer",
            description=description,
            location="Remote",
            employment_type=None,
        ),
    )


def _profile(maximum: int | None) -> CandidateProfile:
    return CandidateProfile(
        client_id="candidate",
        target_roles=RoleTargets(include=["Support Engineer"]),
        skills=Skills(required=["Troubleshooting"]),
        max_required_experience_years=maximum,
    )


def test_mandatory_experience_above_ceiling_rejects_with_explanation() -> None:
    result = match_job(
        _job("Troubleshooting customers requires 3+ years of experience."), _profile(2)
    )
    assert result.decision is MatchDecision.REJECT
    assert result.matcher_version == "deterministic-v5"
    assert result.rejection_reasons == [
        "minimum required experience 3 years exceeds configured maximum 2 years"
    ]


def test_mandatory_experience_equal_to_ceiling_continues() -> None:
    result = match_job(
        _job("Troubleshooting customers requires 2 years of professional experience."),
        _profile(2),
    )
    assert result.decision is MatchDecision.POSSIBLE_MATCH


def test_no_experience_ceiling_preserves_existing_matching_behavior() -> None:
    result = match_job(
        _job("Troubleshooting customers requires 7+ years of experience."), _profile(None)
    )
    assert result.decision is MatchDecision.POSSIBLE_MATCH


def test_no_experience_evidence_does_not_fabricate_rejection() -> None:
    result = match_job(_job("Troubleshooting customer incidents."), _profile(0))
    assert result.decision is MatchDecision.POSSIBLE_MATCH


def test_preferred_experience_is_recorded_but_not_a_hard_requirement() -> None:
    assessment = extract_experience_evidence("3+ years of experience preferred")
    assert assessment.mandatory_minimum_years is None
    assert assessment.evidence[0].kind is ExperienceEvidenceKind.PREFERRED
    assert (
        match_job(
            _job("Troubleshooting expertise. 3+ years of experience preferred."), _profile(2)
        ).decision
        is MatchDecision.POSSIBLE_MATCH
    )


@pytest.mark.parametrize(
    "text",
    [
        "The company was founded 10 years ago.",
        "We support customers in 30 countries.",
        "Work 2 days per week in the office.",
        "We serve 5,000 customers.",
        "Join a 3-person team.",
        "The product has existed for 7 years.",
    ],
)
def test_unrelated_numeric_prose_is_ignored(text: str) -> None:
    assert extract_experience_evidence(text).evidence == ()


def test_strongest_of_several_mandatory_requirements_is_used() -> None:
    assessment = extract_experience_evidence(
        "3+ years technical support experience.\n"
        "1+ year working with Linux.\n"
        "2+ years customer-facing experience."
    )
    assert assessment.mandatory_minimum_years == 3


def test_ambiguous_experience_is_recorded_without_becoming_mandatory() -> None:
    assessment = extract_experience_evidence("Around 4 years of relevant experience.")
    assert assessment.mandatory_minimum_years is None
    assert assessment.evidence[0].kind is ExperienceEvidenceKind.AMBIGUOUS


def _answers(*values: str):
    iterator = iter(values)
    return lambda prompt: next(iterator)


def _profile_answers(*experience: str):
    return _answers(
        "Jane",
        "Support Engineer",
        "Any",
        "4",
        "",
        "Troubleshooting",
        "",
        "",
        "",
        "7",
        *experience,
        "",
        "",
    )


@pytest.mark.parametrize("value", ["", "Any", "any"])
def test_interactive_profile_accepts_any_experience_ceiling(tmp_path, value: str) -> None:
    path = create_profile_interactively(
        input_fn=_profile_answers(value), output_fn=lambda message: None, output_dir=tmp_path
    )
    assert path is not None
    profile = CandidateProfile.model_validate_json(path.read_text())
    assert profile.max_required_experience_years is None


def test_interactive_profile_accepts_integer_experience_ceiling(tmp_path) -> None:
    output: list[str] = []
    path = create_profile_interactively(
        input_fn=_profile_answers("2"), output_fn=output.append, output_dir=tmp_path
    )
    assert path is not None
    profile = CandidateProfile.model_validate_json(path.read_text())
    assert profile.max_required_experience_years == 2
    assert "Maximum required experience: 2 years" in output


def test_invalid_experience_ceiling_reprompts(tmp_path) -> None:
    output: list[str] = []
    path = create_profile_interactively(
        input_fn=_profile_answers("two", "-1", "51", "2"),
        output_fn=output.append,
        output_dir=tmp_path,
    )
    assert path is not None
    assert sum("Please try again" in line for line in output) == 3


def test_matcher_version_is_advanced_before_experience_validation() -> None:
    assert MATCHER_VERSION == "deterministic-v5"


def test_existing_saved_profile_remains_valid_without_a_ceiling() -> None:
    profile = CandidateProfile.model_validate_json(
        Path("config/clients/taiwo_olumide_nigeria.json").read_text(encoding="utf-8")
    )
    assert profile.max_required_experience_years is None
