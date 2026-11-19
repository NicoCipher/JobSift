from job_scout.domain.models import (
    CandidateProfile,
    EmploymentType,
    Job,
    MatchDecision,
    RemoteStatus,
    RoleTargets,
    Seniority,
    Skills,
    UnknownEligibilityPolicy,
)
from job_scout.matching.matcher import match_job
from job_scout.normalization.core import content_fingerprint


def make_job(**changes):
    data = {
        "id": "j1",
        "source": "greenhouse",
        "source_job_id": "1",
        "source_board_id": "acme",
        "title": "Backend Engineer",
        "company": "Acme",
        "description_text": "We use Python and TypeScript.",
        "job_url": "https://example.com/jobs/1",
        "canonical_url": "https://example.com/jobs/1",
        "location_text": "Remote - US",
        "remote_status": RemoteStatus.REMOTE,
        "content_fingerprint": content_fingerprint(
            title="Backend Engineer",
            description="We use Python and TypeScript.",
            location="Remote - US",
            employment_type=None,
        ),
    }
    data.update(changes)
    return Job(**data)


def profile():
    return CandidateProfile(
        client_id="c1",
        target_roles=RoleTargets(include=["Backend Engineer"], exclude=["Manager"]),
        skills=Skills(preferred=["Python", "R"]),
        excluded_keywords=["clearance required"],
    )


def test_transparent_strong_match_and_boundary_skill() -> None:
    result = match_job(make_job(), profile())
    assert result.decision is MatchDecision.STRONG_MATCH
    assert "preferred skill matched: Python" in result.matched_reasons
    assert "preferred skill matched: R" not in result.matched_reasons


def test_explicit_exclusion_rejects() -> None:
    result = match_job(make_job(title="Backend Engineering Manager"), profile())
    assert result.decision is MatchDecision.REJECT
    assert result.rejection_reasons


def test_unknown_remote_is_not_rejected_by_default() -> None:
    p = profile()
    p.remote_policy.allowed = {RemoteStatus.REMOTE}
    assert (
        match_job(make_job(remote_status=RemoteStatus.UNKNOWN), p).decision
        is MatchDecision.NEEDS_REVIEW
    )


def test_seniority_precedence_and_exclusions() -> None:
    p = profile()
    p.target_roles.include = ["Software Engineer", "Engineering Manager"]
    p.excluded_seniority = {
        Seniority.STAFF,
        Seniority.MANAGER,
        Seniority.PRINCIPAL,
        Seniority.LEAD,
        Seniority.DIRECTOR,
    }
    for title in (
        "Senior Staff Software Engineer",
        "Senior Engineering Manager",
        "Principal Software Engineer",
        "Lead Software Engineer",
        "Director of Software Engineering",
    ):
        assert match_job(make_job(title=title), p).decision is MatchDecision.REJECT


def test_title_seniority_outranks_incidental_description_terms() -> None:
    p = profile()
    p.excluded_seniority = {Seniority.MANAGER, Seniority.DIRECTOR}
    job = make_job(
        title="Junior Backend Engineer",
        description_text=(
            "Build with Python and collaborate with senior managers and directors across the company."
        ),
    )
    assert match_job(job, p).decision is MatchDecision.STRONG_MATCH


def test_unknown_country_requires_review_unless_explicitly_allowed() -> None:
    p = profile()
    p.countries = {"United States"}
    assert match_job(make_job(country=None), p).decision is MatchDecision.NEEDS_REVIEW
    p.unknown_country_policy = UnknownEligibilityPolicy.ALLOW
    assert match_job(make_job(country=None), p).decision is MatchDecision.STRONG_MATCH


def test_explicit_wrong_country_rejects_and_no_constraint_does_not_review() -> None:
    p = profile()
    p.countries = {"United States"}
    assert match_job(make_job(country="Canada"), p).decision is MatchDecision.REJECT
    p.countries = set()
    assert match_job(make_job(country=None), p).decision is MatchDecision.STRONG_MATCH


def test_unknown_remote_follows_explicit_policy() -> None:
    p = profile()
    p.remote_policy.allowed = {RemoteStatus.REMOTE}
    p.remote_policy.unknown_policy = UnknownEligibilityPolicy.REJECT
    assert (
        match_job(make_job(remote_status=RemoteStatus.UNKNOWN), p).decision is MatchDecision.REJECT
    )
    p.remote_policy.unknown_policy = UnknownEligibilityPolicy.ALLOW
    assert (
        match_job(make_job(remote_status=RemoteStatus.UNKNOWN), p).decision
        is MatchDecision.STRONG_MATCH
    )


def test_unknown_employment_type_follows_explicit_policy() -> None:
    p = profile()
    p.employment_types = {EmploymentType.FULL_TIME}
    assert match_job(make_job(employment_type=None), p).decision is MatchDecision.NEEDS_REVIEW
    p.unknown_employment_type_policy = UnknownEligibilityPolicy.REJECT
    assert match_job(make_job(employment_type=None), p).decision is MatchDecision.REJECT
