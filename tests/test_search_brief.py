from pathlib import Path

import pytest

from job_scout.domain.models import (
    Job,
    MatchDecision,
    RemoteStatus,
    RuleIntent,
    SearchBrief,
    TargetMarketRule,
    WorkEligibilityRule,
    WorkModeRule,
)
from job_scout.matching.matcher import MATCHER_VERSION, match_job
from job_scout.normalization.core import content_fingerprint
from job_scout.search_brief import create_search_brief_interactively, load_search_brief


def _job(**changes) -> Job:
    description = changes.pop("description_text", "Help customers and troubleshoot systems.")
    title = changes.pop("title", "Support Engineer")
    data = {
        "id": "job-1",
        "source": "greenhouse",
        "source_job_id": "1",
        "source_board_id": "acme",
        "title": title,
        "company": "Acme",
        "description_text": description,
        "job_url": "https://example.com/jobs/1",
        "canonical_url": "https://example.com/jobs/1",
        "remote_status": RemoteStatus.REMOTE,
        "content_fingerprint": content_fingerprint(
            title=title,
            description=description,
            location="Remote",
            employment_type=None,
        ),
    }
    data.update(changes)
    return Job(**data)


def _brief(**changes) -> SearchBrief:
    data = {
        "client_id": "client",
        "target_roles": ["Support Engineer"],
        "preferred_terms": ["Linux"],
    }
    data.update(changes)
    return SearchBrief(**data)


@pytest.mark.parametrize(
    "title",
    [
        "Senior Support Engineer",
        "Staff Support Engineer",
        "Principal Support Engineer",
        "Lead Support Engineer",
    ],
)
def test_individual_contributor_seniority_is_not_rejected_by_default(title: str) -> None:
    result = match_job(_job(title=title), _brief())
    assert result.decision is MatchDecision.POSSIBLE_MATCH


@pytest.mark.parametrize(
    "title",
    [
        "Support Engineering Manager",
        "Director of Technical Support",
        "Head of Support",
        "VP of Support",
        "Chief Support Officer",
    ],
)
def test_management_and_executive_titles_reject_by_default(title: str) -> None:
    result = match_job(_job(title=title), _brief(target_roles=["Support"]))
    assert result.decision is MatchDecision.REJECT
    assert any(
        "management/executive title excluded" in reason for reason in result.rejection_reasons
    )


def test_us_market_is_independent_from_nigeria_residence() -> None:
    brief = _brief(
        candidate_residence="Lagos, Nigeria",
        target_market=TargetMarketRule(countries={"United States"}, intent=RuleIntent.MUST),
        work_eligibility=WorkEligibilityRule(intent=RuleIntent.IGNORE),
    )
    result = match_job(
        _job(
            country="United States",
            description_text="Support customers. U.S. citizenship required.",
        ),
        brief,
    )
    assert result.decision is MatchDecision.POSSIBLE_MATCH
    assert "target market matched: United States" in result.matched_reasons


def test_explicit_work_eligibility_filter_can_still_be_enabled() -> None:
    brief = _brief(
        candidate_residence="Lagos, Nigeria",
        work_eligibility=WorkEligibilityRule(countries={"Nigeria"}, intent=RuleIntent.MUST),
    )
    result = match_job(_job(country="United States"), brief)
    assert result.decision is MatchDecision.REJECT
    assert any("work eligibility countries" in reason for reason in result.rejection_reasons)


def test_missing_preferred_skill_never_rejects() -> None:
    result = match_job(_job(), _brief(preferred_terms=["Active Directory"]))
    assert result.decision is MatchDecision.POSSIBLE_MATCH


def test_missing_must_have_term_rejects() -> None:
    result = match_job(_job(), _brief(must_have_terms=["Active Directory"]))
    assert result.decision is MatchDecision.REJECT
    assert result.rejection_reasons == ["must-have term missing: Active Directory"]


def test_unknown_must_have_evidence_needs_review() -> None:
    result = match_job(_job(description_text=None), _brief(must_have_terms=["Active Directory"]))
    assert result.decision is MatchDecision.NEEDS_REVIEW
    assert (
        "needs review: must-have term cannot be verified: Active Directory"
        in result.matched_reasons
    )


def test_no_experience_ceiling_does_not_reject_seven_year_requirement() -> None:
    result = match_job(
        _job(description_text="Customer support requiring 7+ years of experience."),
        _brief(max_required_experience_years=None),
    )
    assert result.decision is MatchDecision.POSSIBLE_MATCH


def test_configured_experience_ceiling_still_rejects() -> None:
    result = match_job(
        _job(description_text="Customer support requiring 7+ years of experience."),
        _brief(max_required_experience_years=3),
    )
    assert result.decision is MatchDecision.REJECT
    assert result.rejection_reasons == [
        "minimum required experience 7 years exceeds configured maximum 3 years"
    ]


def test_must_target_market_conflict_rejects() -> None:
    brief = _brief(
        target_market=TargetMarketRule(countries={"United States"}, intent=RuleIntent.MUST)
    )
    result = match_job(_job(country="Canada"), brief)
    assert result.decision is MatchDecision.REJECT
    assert any("target market countries" in reason for reason in result.rejection_reasons)


def test_must_work_mode_conflict_rejects() -> None:
    brief = _brief(work_mode=WorkModeRule(modes={RemoteStatus.REMOTE}, intent=RuleIntent.MUST))
    result = match_job(_job(remote_status=RemoteStatus.ONSITE), brief)
    assert result.decision is MatchDecision.REJECT
    assert any("work mode" in reason for reason in result.rejection_reasons)


def test_deterministic_explanations_and_version_are_present() -> None:
    result = match_job(_job(description_text="Troubleshoot Linux systems."), _brief())
    assert result.decision is MatchDecision.STRONG_MATCH
    assert result.matched_reasons == [
        "target role matched: Support Engineer",
        "preferred skill matched: Linux",
    ]
    assert result.matcher_version == MATCHER_VERSION == "deterministic-v5"


def test_old_saved_profile_has_safe_explicit_compatibility_migration() -> None:
    brief = load_search_brief(Path("config/clients/taiwo_olumide_nigeria.json"))
    assert brief.work_eligibility.intent is RuleIntent.MUST
    assert brief.work_eligibility.countries == {"Nigeria"}
    assert brief.target_market.intent is RuleIntent.IGNORE
    assert brief.must_have_terms == ["Troubleshooting"]


def _answers(*values: str):
    iterator = iter(values)
    return lambda prompt: next(iterator)


def test_operator_flow_creates_basic_sourcing_brief(tmp_path) -> None:
    output: list[str] = []
    path = create_search_brief_interactively(
        input_fn=_answers(
            "Jane",
            "Technical Support Engineer, IT Support",
            "US",
            "Lagos, Nigeria",
            "1",
            "",
            "Sales Support",
            "",
            "Networking, Linux",
            "",
            "",
            "Operator brief",
            "",
        ),
        output_fn=output.append,
        output_dir=tmp_path,
    )
    assert path is not None
    brief = SearchBrief.model_validate_json(path.read_text(encoding="utf-8"))
    assert brief.target_market.countries == {"United States"}
    assert brief.candidate_residence == "Lagos, Nigeria"
    assert brief.work_eligibility.intent is RuleIntent.IGNORE
    assert brief.management_roles is RuleIntent.AVOID
    assert brief.max_required_experience_years is None
    assert any("Search brief summary" in line for line in output)
    assert any("Candidate residence: Lagos, Nigeria" in line for line in output)


def test_operator_flow_exposes_advanced_filters_only_when_requested(tmp_path) -> None:
    path = create_search_brief_interactively(
        input_fn=_answers(
            "Jane",
            "Support Engineer",
            "US",
            "Nigeria",
            "4",
            "n",
            "",
            "Troubleshooting",
            "Linux",
            "clearance required",
            "y",
            "2",
            "y",
            "Nigeria",
            "7",
            "",
            "",
        ),
        output_fn=lambda message: None,
        output_dir=tmp_path,
    )
    assert path is not None
    brief = SearchBrief.model_validate_json(path.read_text(encoding="utf-8"))
    assert brief.max_required_experience_years == 2
    assert brief.work_eligibility.intent is RuleIntent.MUST
    assert brief.work_eligibility.countries == {"Nigeria"}


def test_committed_operator_style_taiwo_brief_has_new_semantics() -> None:
    brief = load_search_brief("config/search_briefs/taiwo_operator_sourcing_v1.json")
    assert brief.target_market.countries == {"United States"}
    assert brief.target_market.intent is RuleIntent.MUST
    assert brief.candidate_residence == "Lagos, Nigeria"
    assert brief.work_eligibility.intent is RuleIntent.IGNORE
    assert brief.management_roles is RuleIntent.AVOID
    assert brief.excluded_seniority == set()
    assert brief.max_required_experience_years is None
    assert "Troubleshooting" in brief.preferred_terms
    assert brief.must_have_terms == []
