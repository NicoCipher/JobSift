import pytest

from job_scout.domain.models import EmploymentType, RemoteStatus
from job_scout.normalization.core import (
    canonicalize_url,
    classify_remote,
    content_fingerprint,
    html_to_text,
    normalize_title,
)


def test_description_cleanup_preserves_structure() -> None:
    assert (
        html_to_text("&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;&lt;li&gt;Python&lt;/li&gt;")
        == "Hello & welcome\nPython"
    )


def test_url_canonicalization_removes_only_tracking() -> None:
    assert (
        canonicalize_url("HTTPS://Example.COM/jobs/1/?utm_source=x&gh_jid=1#apply")
        == "https://example.com/jobs/1?gh_jid=1"
    )


def test_description_cleanup_removes_script_and_style_content() -> None:
    value = "<style>.hidden{}</style><p>Useful role.</p><script>alert('x')</script>"
    assert html_to_text(value) == "Useful role."


def test_title_and_remote_conflict() -> None:
    assert normalize_title("Full-Stack Engineer") == "full stack engineer"
    assert classify_remote("Remote", "Work 3 days per week in-office") is RemoteStatus.HYBRID


def test_office_remote_evidence_and_specific_conflict() -> None:
    assert classify_remote(None, None, ["Remote US"], []) is RemoteStatus.REMOTE
    assert (
        classify_remote("Remote - United States", "This role is hybrid, 3 days per week in office")
        is RemoteStatus.HYBRID
    )
    assert classify_remote(None, None, ["Remote Engineering"], []) is RemoteStatus.UNKNOWN


def test_fingerprint_ignores_collection_time() -> None:
    args = {
        "title": "Engineer",
        "description": "Build things",
        "location": "Remote",
        "employment_type": EmploymentType.FULL_TIME,
    }
    assert content_fingerprint(**args) == content_fingerprint(**args)

def test_explicit_city_state_beats_remote_boilerplate() -> None:
    description = (
        "Excellent communication skills in a fully remote environment. "
        "#LI-Remote"
    )
    assert classify_remote("Los Angeles, CA", description) is RemoteStatus.ONSITE
    assert (
        classify_remote("Remote - United States", description)
        is RemoteStatus.REMOTE
    )




@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("This is not a remote role.", RemoteStatus.UNKNOWN),
        ("This position is not fully remote.", RemoteStatus.UNKNOWN),
        ("Remote work is not available for this team.", RemoteStatus.UNKNOWN),
        ("The role is onsite, not remote.", RemoteStatus.ONSITE),
        ("This job is no longer remote.", RemoteStatus.UNKNOWN),
    ],
)
def test_negated_remote_description_is_not_affirmative(
    description: str, expected: RemoteStatus
) -> None:
    assert classify_remote(None, description) is expected


@pytest.mark.parametrize(
    "description",
    [
        "This is a fully remote role.",
        "This position is 100% remote.",
        "The job is remote-only.",
        "You will work remotely from anywhere in the United States.",
        "This position is fully remote and open to US candidates.",
    ],
)
def test_explicit_fully_remote_description_is_affirmative(description: str) -> None:
    assert classify_remote(None, description) is RemoteStatus.REMOTE


def test_bare_remote_mention_in_description_is_unknown() -> None:
    assert classify_remote(None, "Remote collaboration is part of our culture.") is RemoteStatus.UNKNOWN
    assert classify_remote(None, "This is a remote role.") is RemoteStatus.UNKNOWN

@pytest.mark.parametrize(
    "description",
    [
        "You cannot work fully remotely.",
        "You can't work fully remotely.",
        "You are not able to work fully remotely.",
        "This role does not allow you to work remotely.",
    ],
)
def test_negated_fully_remote_wording_is_rejected(description: str) -> None:
    assert classify_remote(None, description) is RemoteStatus.UNKNOWN


@pytest.mark.parametrize(
    "description",
    [
        "This is a fully remote role.",
        "This position is 100% remote.",
        "The job is remote-only.",
        "You will work remotely from anywhere in the United States.",
        "This position is fully remote and open to US candidates.",
    ],
)
def test_role_or_applicant_specific_remote_evidence_passes(description: str) -> None:
    assert classify_remote(None, description) is RemoteStatus.REMOTE


@pytest.mark.parametrize(
    "description",
    [
        "You will support customers working fully remotely.",
        "Our customers work fully remotely, but this role is based in Boston.",
        "The team supports clients in a fully remote environment.",
    ],
)
def test_third_party_remote_arrangement_does_not_classify_role(
    description: str,
) -> None:
    assert classify_remote(None, description) is RemoteStatus.UNKNOWN
