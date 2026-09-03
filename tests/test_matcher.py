from job_scout.domain.models import (
    CandidateProfile,
    Job,
    MatchDecision,
    RemoteStatus,
    RoleTargets,
    Skills,
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
        is not MatchDecision.REJECT
    )
