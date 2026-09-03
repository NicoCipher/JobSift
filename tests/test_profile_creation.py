import httpx

from job_scout.collectors.greenhouse import GreenhouseCollector
from job_scout.domain.models import (
    CandidateProfile,
    RemoteStatus,
    SourceTarget,
    UnknownEligibilityPolicy,
)
from job_scout.orchestration.pipeline import run_pipeline
from job_scout.profile import create_profile_interactively
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
            "Remote",
            "Staff, Principal, Manager, Director",
            "Python",
            "TypeScript, AWS",
            "Engineering Manager",
            "security clearance required",
            "Any",
            "First validation client",
        ),
        output_fn=output.append,
        output_dir=tmp_path,
    )
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
            "Remote",
            "Manager",
            "Python",
            "TypeScript",
            "Engineering Manager",
            "",
            "Any",
            "",
        ),
        output_fn=lambda message: None,
        output_dir=tmp_path / "profiles",
    )
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
