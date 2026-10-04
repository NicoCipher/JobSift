from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from job_scout.collectors.greenhouse import GreenhouseCollector
from job_scout.collectors.smartrecruiters import SmartRecruitersCollector
from job_scout.domain.models import CollectionStatus, SourceTarget
from job_scout.incremental_collection import IncrementalTargetState


def target() -> SourceTarget:
    return SourceTarget(board_id="acme", company="Acme Inc", employer_id="acme")


def greenhouse_job(job_id: int, *, first_published: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": job_id,
        "title": f"Software Engineer {job_id}",
        "location": {"name": "Remote"},
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        "content": "<p>Build reliable software.</p>",
        "updated_at": "2026-10-04T09:00:00Z",
        "departments": [{"name": "Engineering"}],
        "offices": [],
    }
    if first_published is not None:
        payload["first_published"] = first_published
    return payload


def smartrecruiters_posting(
    job_id: str,
    *,
    released_date: str | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": job_id,
        "uuid": f"12345678-1234-4234-8234-{int(job_id):012d}",
        "name": f"Software Engineer {job_id}",
        "company": {"identifier": "acme", "name": "Acme Inc"},
        "location": {
            "city": "Remote",
            "country": "us",
            "remote": True,
        },
        "ref": f"https://api.smartrecruiters.com/v1/companies/acme/postings/{job_id}",
    }
    if released_date is not None:
        payload["releasedDate"] = released_date
    return payload


def test_greenhouse_incremental_hydrates_new_unknown_age_posting_once() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/v1/boards/acme/jobs":
            return httpx.Response(
                200,
                json={"jobs": [greenhouse_job(1)]},
                request=request,
            )
        assert request.url.path == "/v1/boards/acme/jobs/1"
        return httpx.Response(
            200,
            json={
                **greenhouse_job(1),
                "first_published": "2026-10-04T09:30:00Z",
            },
            request=request,
        )

    state = IncrementalTargetState(source="greenhouse", board_id="acme")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        collector = GreenhouseCollector(client, incremental_states=[state])
        result = collector.collect(target())

    assert result.status is CollectionStatus.SUCCESS
    assert len(result.jobs) == 1
    assert result.jobs[0].posted_at == datetime(2026, 10, 4, 9, 30, tzinfo=UTC)
    assert collector.last_counts["detail_requests"] == 1
    assert collector.last_counts["incremental_new_ids"] == 1
    assert requests == ["/v1/boards/acme/jobs", "/v1/boards/acme/jobs/1"]


def test_greenhouse_incremental_reuses_active_freshness_without_detail_read() -> None:
    posted_at = datetime.now(UTC)
    state = IncrementalTargetState(
        source="greenhouse",
        board_id="acme",
        known_source_job_ids=["1"],
        active_posted_at_by_source_job_id={"1": posted_at},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/boards/acme/jobs"
        return httpx.Response(
            200,
            json={"jobs": [greenhouse_job(1)]},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        collector = GreenhouseCollector(client, incremental_states=[state])
        result = collector.collect(target())

    assert result.status is CollectionStatus.SUCCESS
    assert len(result.jobs) == 1
    assert result.jobs[0].posted_at == posted_at
    assert collector.last_counts["detail_requests"] == 0
    assert collector.last_counts["reused_posted_at"] == 1


def test_greenhouse_incremental_does_not_rehydrate_known_unknown_age_posting() -> None:
    state = IncrementalTargetState(
        source="greenhouse",
        board_id="acme",
        known_source_job_ids=["1"],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/boards/acme/jobs"
        return httpx.Response(
            200,
            json={"jobs": [greenhouse_job(1)]},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        collector = GreenhouseCollector(client, incremental_states=[state])
        result = collector.collect(target())

    assert result.status is CollectionStatus.SUCCESS
    assert result.jobs == []
    assert collector.last_counts["detail_requests"] == 0
    assert collector.last_counts["suppressed_known_unknown_age"] == 1


def test_smartrecruiters_incremental_skips_stale_detail_hydration() -> None:
    state = IncrementalTargetState(source="smartrecruiters", board_id="acme")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/companies/acme/postings"
        return httpx.Response(
            200,
            json={
                "limit": 100,
                "offset": 0,
                "totalFound": 1,
                "content": [
                    smartrecruiters_posting(
                        "1",
                        released_date="2020-01-01T00:00:00Z",
                    )
                ],
            },
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        collector = SmartRecruitersCollector(client, incremental_states=[state])
        result = collector.collect(target())

    assert result.status is CollectionStatus.SUCCESS
    assert result.jobs == []
    assert collector.last_counts["detail_requests"] == 0
    assert collector.last_counts["incremental_stale_suppressed"] == 1


def test_incremental_state_rejects_active_timestamp_for_unknown_identity() -> None:
    with pytest.raises(ValueError, match="known provider id"):
        IncrementalTargetState(
            source="greenhouse",
            board_id="acme",
            active_posted_at_by_source_job_id={"1": datetime.now(UTC)},
        )
