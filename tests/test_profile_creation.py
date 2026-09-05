import httpx
import pytest

from job_scout.collectors.greenhouse import GreenhouseCollector
from job_scout.domain.models import (
    CandidateProfile,
    Job,
    MatchDecision,
    RemoteStatus,
    SourceTarget,
    UnknownEligibilityPolicy,
)
from job_scout.matching.matcher import match_job
from job_scout.normalization.core import content_fingerprint
from job_scout.orchestration.pipeline import run_pipeline
from job_scout.profile import canonicalize_country_input, create_profile_interactively
from job_scout.storage.sqlite import SQLiteRepository


def answers(*values: str):
    iterator = iter(values)
    return lambda prompt: next(iterator)


def test_interactive_profile_creation_is_valid_and_conservative(tmp_path) -> None:
    output: list[str] = []
    path = create_profile_interactively(
        input_fn=answers(
            "Jane Doe",
            "Backend Engineer, Software Engineer",
            "United States",
            "1",
            "Staff, Principal, Manager, Director",
            "Python",
            "TypeScript, AWS",
            "Engineering Manager",
            "security clearance required",
            "7",
            "",
            "First validation client",
            "",
        ),
        output_fn=output.append,
        output_dir=tmp_path,
    )
    assert path is not None
    profile = CandidateProfile.model_validate_json(path.read_text())
    assert profile.client_id == "jane_doe"
    assert profile.remote_policy.allowed == {RemoteStatus.REMOTE}
    assert profile.remote_policy.unknown_policy is UnknownEligibilityPolicy.REVIEW
    assert profile.unknown_country_policy is UnknownEligibilityPolicy.REVIEW
    assert profile.employment_types == set()
    assert any("Profile summary" in line for line in output)
    assert any("Employment type: Any" in line for line in output)


def test_generated_profile_runs_fixture_to_csv(tmp_path) -> None:
    path = create_profile_interactively(
        input_fn=answers(
            "Jane",
            "Backend Engineer",
            "United States",
            "1",
            "Manager",
            "Python",
            "TypeScript",
            "Engineering Manager",
            "",
            "7",
            "",
            "",
            "",
        ),
        output_fn=lambda message: None,
        output_dir=tmp_path / "profiles",
    )
    assert path is not None
    profile = CandidateProfile.model_validate_json(path.read_text())
    payload = {
        "jobs": [
            {
                "id": 1,
                "title": "Backend Engineer",
                "location": {"name": "Remote - United States"},
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                "content": "&lt;p&gt;Build APIs with Python and TypeScript.&lt;/p&gt;",
            }
        ]
    }
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload, request=request)
    )
    summary = run_pipeline(
        collector=GreenhouseCollector(httpx.Client(transport=transport)),
        target=SourceTarget(board_id="acme", company="Acme"),
        profile=profile,
        repository=SQLiteRepository(tmp_path / "jobs.db"),
        csv_path=tmp_path / "jobs.csv",
    )
    assert summary.matched == 1
    assert summary.exported == 1


@pytest.mark.parametrize(
    "value", ["US", "USA", "U.S.", "U.S.A.", "United States", "United States of America"]
)
def test_us_aliases_are_canonicalized(value: str) -> None:
    assert canonicalize_country_input(value) == "United States"


@pytest.mark.parametrize("value", ["", "   ", "Any", "any"])
def test_blank_or_any_removes_country_constraint(value: str) -> None:
    assert canonicalize_country_input(value) is None


def _basic_answers(
    *,
    country="US",
    work=("1",),
    seniority=("Staff",),
    employment=("7",),
    experience=("",),
    confirmation="",
):
    return answers(
        "Jane",
        "Backend Engineer",
        country,
        *work,
        *seniority,
        "Python",
        "TypeScript",
        "Engineering Manager",
        "clearance required",
        *employment,
        *experience,
        "",
        confirmation,
    )


def test_invalid_choices_reprompt_instead_of_crashing(tmp_path) -> None:
    output: list[str] = []
    path = create_profile_interactively(
        input_fn=_basic_answers(work=("9", "1"), employment=("0", "7")),
        output_fn=output.append,
        output_dir=tmp_path,
    )
    assert path is not None
    assert sum("Please try again" in line for line in output) == 2


def test_blank_required_inputs_and_invalid_seniority_reprompt(tmp_path) -> None:
    output: list[str] = []
    path = create_profile_interactively(
        input_fn=answers(
            "",
            "Jane",
            "",
            "Backend Engineer",
            "US",
            "1",
            "Wizard",
            "Staff",
            "Python",
            "",
            "",
            "",
            "7",
            "",
            "",
            "",
        ),
        output_fn=output.append,
        output_dir=tmp_path,
    )
    assert path is not None
    assert sum("Please try again" in line for line in output) == 3


def test_declining_confirmation_creates_no_file(tmp_path) -> None:
    path = create_profile_interactively(
        input_fn=_basic_answers(confirmation="n"),
        output_fn=lambda message: None,
        output_dir=tmp_path,
    )
    assert path is None
    assert list(tmp_path.iterdir()) == []


def test_existing_profile_is_not_overwritten(tmp_path) -> None:
    existing = tmp_path / "jane.json"
    existing.write_text("keep me", encoding="utf-8")
    output: list[str] = []
    path = create_profile_interactively(
        input_fn=_basic_answers(),
        output_fn=output.append,
        output_dir=tmp_path,
    )
    assert path is None
    assert existing.read_text(encoding="utf-8") == "keep me"
    assert any("already exists" in line for line in output)


def test_summary_and_next_command_are_operator_readable(tmp_path) -> None:
    output: list[str] = []
    path = create_profile_interactively(
        input_fn=_basic_answers(),
        output_fn=output.append,
        output_dir=tmp_path,
    )
    assert path is not None
    rendered = "\n".join(output)
    assert "Client: Jane" in rendered
    assert "Country: United States" in rendered
    assert "Excluded titles: Engineering Manager" in rendered
    assert "Profile saved:" in rendered
    assert "python -m job_scout collect" in rendered
    assert "--client" in rendered and "--csv exports/jane.csv" in rendered


def test_generated_us_profile_matches_canonical_us_job(tmp_path) -> None:
    path = create_profile_interactively(
        input_fn=_basic_answers(),
        output_fn=lambda message: None,
        output_dir=tmp_path,
    )
    assert path is not None
    profile = CandidateProfile.model_validate_json(path.read_text())
    job = Job(
        id="job-1",
        source="greenhouse",
        source_job_id="1",
        source_board_id="acme",
        title="Backend Engineer",
        company="Acme",
        description_text="Build with Python and TypeScript.",
        job_url="https://example.com/jobs/1",
        canonical_url="https://example.com/jobs/1",
        country="United States",
        remote_status=RemoteStatus.REMOTE,
        content_fingerprint=content_fingerprint(
            title="Backend Engineer",
            description="Build with Python and TypeScript.",
            location="Remote",
            employment_type=None,
        ),
    )
    assert match_job(job, profile).decision is MatchDecision.STRONG_MATCH
