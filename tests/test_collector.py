import json
from pathlib import Path

import httpx

from job_scout.collectors.greenhouse import GreenhouseCollector
from job_scout.domain.models import CollectionStatus, RemoteStatus, SourceTarget

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


def test_malformed_payload_is_not_empty_success() -> None:
    result = collector(payload={"wrong": []}).collect(SourceTarget(board_id="acme", company="Acme"))
    assert result.status is CollectionStatus.PARSE_FAILURE
    assert result.errors


def test_rate_limit_is_explicit() -> None:
    result = collector(status=429).collect(SourceTarget(board_id="acme", company="Acme"))
    assert result.status is CollectionStatus.RATE_LIMITED
