from job_scout.domain.models import Job, MatchDecision, SearchBrief
from job_scout.normalization.core import content_fingerprint
from scripts.strict_profile_matcher import match_profile_job


def brief(client_id="nicocipher"):
    return SearchBrief(
        client_id=client_id,
        target_roles=["Software Engineer", "Backend Engineer", "Technical Support Specialist"],
        target_market={
            "countries": ["United States"],
            "intent": "must",
            "unknown_policy": "reject",
        },
        work_mode={
            "modes": ["remote"],
            "intent": "must",
            "unknown_policy": "reject",
        },
        excluded_titles=["Production Support"],
        candidate_residence="Lagos, Nigeria",
    )


def job(title="Software Engineer", location="Remote, US", description="Build software.",
        country="United States", eligible=None):
    return Job(
        id="job-1",
        source="greenhouse",
        source_job_id="1",
        source_board_id="acme",
        title=title,
        company="Acme",
        description_text=description,
        job_url="https://example.com/jobs/1",
        canonical_url="https://example.com/jobs/1",
        location_text=location,
        country=country,
        eligible_countries=eligible or {"United States"},
        remote_status="remote",
        content_fingerprint=content_fingerprint(
            title=title, description=description, location=location, employment_type=None
        ),
    )


def test_explicit_canada_location_overrides_conflicting_us_provider_metadata():
    result = match_profile_job(
        job(location="Remote, Canada", eligible={"United States"}), brief()
    )
    assert result.decision is MatchDecision.REJECT
    assert any("Canada" in reason for reason in result.rejection_reasons)


def test_explicit_onsite_description_overrides_remote_metadata():
    result = match_profile_job(
        job(
            location="New York, US",
            description="This role is on-site in New York. Remote work may be considered.",
        ),
        brief(),
    )
    assert result.decision is MatchDecision.REJECT
    assert any("on-site" in reason for reason in result.rejection_reasons)


def test_us_residence_restriction_rejects_candidate_in_nigeria():
    result = match_profile_job(
        job(description="Applicants must be based in the United States to be considered."),
        brief(),
    )
    assert result.decision is MatchDecision.REJECT
    assert any("reside or be based" in reason for reason in result.rejection_reasons)


def test_software_engineer_in_test_requires_human_review():
    result = match_profile_job(job(title="Principal Software Engineer in Test"), brief())
    assert result.decision is MatchDecision.NEEDS_REVIEW
    assert any("test/QA-specialist" in reason for reason in result.matched_reasons)


def test_production_support_is_excluded_by_profile_brief():
    result = match_profile_job(job(title="Senior Software Engineer, Production Support"), brief())
    assert result.decision is MatchDecision.REJECT
    assert any("Production Support" in reason for reason in result.rejection_reasons)


def test_other_clients_keep_generic_support_role_matching():
    result = match_profile_job(
        job(title="Technical Support Specialist"),
        brief(client_id="other-client"),
    )
    assert result.decision is MatchDecision.POSSIBLE_MATCH
