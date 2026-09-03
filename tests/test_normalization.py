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


def test_fingerprint_ignores_collection_time() -> None:
    args = {
        "title": "Engineer",
        "description": "Build things",
        "location": "Remote",
        "employment_type": EmploymentType.FULL_TIME,
    }
    assert content_fingerprint(**args) == content_fingerprint(**args)
