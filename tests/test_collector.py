import json
from pathlib import Path

import httpx

from job_scout.collectors.greenhouse import GreenhouseCollector
from job_scout.domain.models import CollectionStatus, EmploymentType, RemoteStatus, SourceTarget

FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_jobs.json"


def collector(status=200, payload=None):
    data = payload if payload is not None else json.loads(FIXTURE.read_text())
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, json=data, request=request)
    )
    return GreenhouseCollector(httpx.Client(transport=transport))


def test_greenhouse_fixture_normalizes() -> None:
    result = collector().collect(SourceTarget(board_id="acme", company="Acme"))
    assert result.status is CollectionStatus.SUCCESS
    assert len(result.jobs) == 2
    assert result.jobs[0].remote_status is RemoteStatus.REMOTE
    assert str(result.jobs[0].canonical_url) == "https://boards.greenhouse.io/acme/jobs/101"
    assert result.jobs[0].offices == ["Remote US"]
    assert result.jobs[0].posted_at is not None
    assert result.jobs[0].country == "United States"


def test_malformed_payload_is_not_empty_success() -> None:
    result = collector(payload={"wrong": []}).collect(SourceTarget(board_id="acme", company="Acme"))
    assert result.status is CollectionStatus.PARSE_FAILURE
    assert result.errors


def test_rate_limit_is_explicit() -> None:
    result = collector(status=429).collect(SourceTarget(board_id="acme", company="Acme"))
    assert result.status is CollectionStatus.RATE_LIMITED


def test_throttled_403_is_distinct_from_forbidden_403() -> None:
    target = SourceTarget(board_id="acme", company="Acme")
    limited = httpx.MockTransport(
        lambda request: httpx.Response(403, text="Rate limit exceeded", request=request)
    )
    forbidden = httpx.MockTransport(
        lambda request: httpx.Response(403, text="Forbidden", request=request)
    )
    assert (
        GreenhouseCollector(httpx.Client(transport=limited)).collect(target).status
        is CollectionStatus.RATE_LIMITED
    )
    assert (
        GreenhouseCollector(httpx.Client(transport=forbidden)).collect(target).status
        is CollectionStatus.FORBIDDEN
    )


def test_malformed_job_is_quarantined_without_losing_valid_jobs() -> None:
    payload = json.loads(FIXTURE.read_text())
    payload["jobs"].append({"id": 999, "title": ""})
    result = collector(payload=payload).collect(SourceTarget(board_id="acme", company="Acme"))
    assert result.status is CollectionStatus.PARTIAL
    assert len(result.jobs) == 2
    assert result.errors


def test_raw_metadata_is_allowlisted() -> None:
    payload = json.loads(FIXTURE.read_text())
    payload["jobs"][0]["recruiter"] = {"email": "private@example.com"}
    payload["jobs"][0]["candidate_fields"] = {"ssn": "000"}
    result = collector(payload=payload).collect(SourceTarget(board_id="acme", company="Acme"))
    metadata = result.jobs[0].raw_metadata
    assert "recruiter" not in metadata
    assert "candidate_fields" not in metadata


def test_vague_remote_location_keeps_country_unknown() -> None:
    payload = json.loads(FIXTURE.read_text())
    payload["jobs"] = [payload["jobs"][0]]
    payload["jobs"][0]["location"] = {"name": "Remote"}
    payload["jobs"][0]["offices"] = []
    job = collector(payload=payload).collect(SourceTarget(board_id="acme", company="Acme")).jobs[0]
    assert job.remote_status is RemoteStatus.REMOTE
    assert job.country is None


def test_office_location_provides_deterministic_country_evidence() -> None:
    payload = json.loads(FIXTURE.read_text())
    payload["jobs"] = [payload["jobs"][0]]
    payload["jobs"][0]["location"] = {"name": "Remote"}
    payload["jobs"][0]["offices"] = [
        {"name": "East Coast", "location": "New York, NY, United States"}
    ]
    job = collector(payload=payload).collect(SourceTarget(board_id="acme", company="Acme")).jobs[0]
    assert job.country == "United States"


def test_employment_type_remains_unknown_without_supported_metadata() -> None:
    payload = json.loads(FIXTURE.read_text())
    payload["jobs"] = [payload["jobs"][0]]
    payload["jobs"][0]["metadata"] = [{"name": "Team", "value": "Full Time"}]
    job = collector(payload=payload).collect(SourceTarget(board_id="acme", company="Acme")).jobs[0]
    assert job.employment_type is None


def test_exact_employment_type_metadata_is_normalized() -> None:
    payload = json.loads(FIXTURE.read_text())
    payload["jobs"] = [payload["jobs"][0]]
    payload["jobs"][0]["metadata"] = [{"id": 7, "name": "Employment Type", "value": "Full-time"}]
    job = collector(payload=payload).collect(SourceTarget(board_id="acme", company="Acme")).jobs[0]
    assert job.employment_type is EmploymentType.FULL_TIME
    assert job.raw_metadata["employment_type_evidence"] == {
        "field": "Employment Type",
        "value": "Full-time",
    }
