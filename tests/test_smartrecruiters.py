from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from job_scout.collectors.smartrecruiters import SmartRecruitersCollector
from job_scout.domain.models import CollectionStatus, EmploymentType, RemoteStatus, SourceTarget


def posting(job_id: str, *, remote: bool = True) -> dict[str, object]:
    return {
        "id": job_id,
        "uuid": f"12345678-1234-4234-8234-{int(job_id):012d}",
        "name": f"Software Engineer {job_id}",
        "refNumber": f"REF-{job_id}",
        "company": {"identifier": "acme", "name": "Acme Inc"},
        "releasedDate": "2026-10-01T12:00:00Z",
        "location": {
            "city": "San Francisco",
            "region": "CA",
            "country": "us",
            "remote": remote,
        },
        "department": {"id": "eng", "label": "Engineering"},
        "typeOfEmployment": {"id": "full", "label": "Full-time"},
        "experienceLevel": {"id": "mid", "label": "Mid-Senior Level"},
        "ref": f"https://api.smartrecruiters.com/v1/companies/acme/postings/{job_id}",
    }


def detail(job_id: str, *, remote: bool = True) -> dict[str, object]:
    return {
        **posting(job_id, remote=remote),
        "postingUrl": f"https://jobs.smartrecruiters.com/Acme/{job_id}-software-engineer",
        "applyUrl": f"https://jobs.smartrecruiters.com/Acme/{job_id}-software-engineer?oga=true",
        "active": True,
        "jobAd": {
            "sections": {
                "companyDescription": {"text": "<p>Acme builds reliable software.</p>"},
                "jobDescription": {"text": "<p>Build production APIs and services.</p>"},
                "qualifications": {"text": "<p>Python and distributed systems.</p>"},
                "additionalInformation": {"text": "<p>Remote-friendly engineering team.</p>"},
            }
        },
    }


def target() -> SourceTarget:
    return SourceTarget(board_id="acme", company="Acme Inc", employer_id="acme")


def test_smartrecruiters_paginates_then_hydrates_canonical_postings() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        path = request.url.path
        if path == "/v1/companies/acme/postings":
            offset = int(request.url.params["offset"])
            payload = {
                "limit": 2,
                "offset": offset,
                "totalFound": 3,
                "content": [posting("1"), posting("2")] if offset == 0 else [posting("3")],
            }
            return httpx.Response(200, json=payload, request=request)
        job_id = path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=detail(job_id), request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        collector = SmartRecruitersCollector(client)
        collector.page_size = 2
        result = collector.collect(target())

    assert result.status is CollectionStatus.SUCCESS
    assert result.raw_postings_received == 3
    assert len(result.jobs) == 3
    assert {
        key: collector.last_counts[key]
        for key in (
            "list_requests",
            "detail_requests",
            "indexed",
            "hydrated",
            "quarantined",
            "vanished",
        )
    } == {
        "list_requests": 2,
        "detail_requests": 3,
        "indexed": 3,
        "hydrated": 3,
        "quarantined": 0,
        "vanished": 0,
    }
    assert all("destination=PUBLIC" in url for url in requests[:2])
    job = result.jobs[0]
    assert job.posted_at == datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    assert job.country == "United States"
    assert job.eligible_countries == {"United States"}
    assert job.remote_status is RemoteStatus.REMOTE
    assert job.employment_type is EmploymentType.FULL_TIME
    assert job.department == "Engineering"
    assert job.description_text and "distributed systems" in job.description_text
    assert str(job.canonical_url) == "https://jobs.smartrecruiters.com/Acme/1-software-engineer"
    assert (
        str(job.apply_url) == "https://jobs.smartrecruiters.com/Acme/1-software-engineer?oga=true"
    )
    assert job.raw_metadata["provider_contract"] == "smartrecruiters-posting-api-v1"


def test_smartrecruiters_strips_board_once_for_request_validation_and_job_identity() -> None:
    spaced_target = SourceTarget(
        board_id=" Acme ",
        company="Acme Inc",
        employer_id="acme",
    )
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/v1/companies/Acme/postings":
            return httpx.Response(
                200,
                json={
                    "limit": 100,
                    "offset": 0,
                    "totalFound": 1,
                    "content": [posting("1")],
                },
                request=request,
            )
        assert request.url.path == "/v1/companies/Acme/postings/1"
        return httpx.Response(200, json=detail("1"), request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = SmartRecruitersCollector(client).collect(spaced_target)

    assert result.status is CollectionStatus.SUCCESS
    assert len(result.jobs) == 1
    assert result.jobs[0].source_board_id == "acme"
    assert requests == [
        "/v1/companies/Acme/postings",
        "/v1/companies/Acme/postings/1",
    ]


def test_smartrecruiters_public_list_rate_limit_is_explicit() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(429, text="Too many requests", request=request)
    )
    with httpx.Client(transport=transport) as client:
        result = SmartRecruitersCollector(client).collect(target())

    assert result.status is CollectionStatus.RATE_LIMITED
    assert result.jobs == []
    assert result.errors == ["HTTP 429"]


def test_smartrecruiters_invalid_company_identifier_is_not_empty_success() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(404, text="Not found", request=request)
    )
    with httpx.Client(transport=transport) as client:
        result = SmartRecruitersCollector(client).collect(target())

    assert result.status is CollectionStatus.INVALID_TARGET
    assert result.errors


def test_smartrecruiters_bad_detail_is_quarantined_without_losing_valid_jobs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/companies/acme/postings":
            return httpx.Response(
                200,
                json={
                    "limit": 100,
                    "offset": 0,
                    "totalFound": 2,
                    "content": [posting("1"), posting("2")],
                },
                request=request,
            )
        if request.url.path.endswith("/1"):
            return httpx.Response(200, json=detail("1"), request=request)
        return httpx.Response(200, json={"id": "2", "name": "Broken"}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = SmartRecruitersCollector(client).collect(target())

    assert result.status is CollectionStatus.PARTIAL
    assert result.raw_postings_received == 2
    assert len(result.jobs) == 1
    assert result.jobs[0].source_job_id == "1"
    assert result.errors


@pytest.mark.parametrize(
    "apply_url",
    [
        "https://example.com/unrelated",
        "https://jobs.smartrecruiters.com/Other/1-software-engineer",
        "https://jobs.smartrecruiters.com/acme/2-software-engineer",
        "https://jobs.smartrecruiters.com.evil/acme/1-software-engineer",
        "http://jobs.smartrecruiters.com/acme/1-software-engineer",
        "https://jobs.smartrecruiters.com/acme/1-software-engineer/not-apply",
    ],
)
def test_smartrecruiters_rejects_unbound_apply_url_before_normalizing(apply_url: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/companies/acme/postings":
            return httpx.Response(
                200,
                json={
                    "limit": 100,
                    "offset": 0,
                    "totalFound": 1,
                    "content": [posting("1")],
                },
                request=request,
            )
        body = detail("1")
        body["applyUrl"] = apply_url
        return httpx.Response(200, json=body, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        collector = SmartRecruitersCollector(client)
        result = collector.collect(target())

    assert result.status is CollectionStatus.PARTIAL
    assert result.jobs == []
    assert collector.last_counts["hydrated"] == 0
    assert collector.last_counts["quarantined"] == 1
    assert "apply URL does not match requested posting" in result.errors[0]


def test_smartrecruiters_false_remote_flag_does_not_invent_onsite() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/companies/acme/postings":
            return httpx.Response(
                200,
                json={
                    "limit": 100,
                    "offset": 0,
                    "totalFound": 1,
                    "content": [posting("1", remote=False)],
                },
                request=request,
            )
        return httpx.Response(200, json=detail("1", remote=False), request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        job = SmartRecruitersCollector(client).collect(target()).jobs[0]

    assert job.remote_status is RemoteStatus.UNKNOWN


def test_smartrecruiters_detail_404_is_counted_as_vanished_not_target_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/companies/acme/postings":
            return httpx.Response(
                200,
                json={
                    "limit": 100,
                    "offset": 0,
                    "totalFound": 2,
                    "content": [posting("1"), posting("2")],
                },
                request=request,
            )
        if request.url.path.endswith("/1"):
            return httpx.Response(200, json=detail("1"), request=request)
        return httpx.Response(404, text="Posting no longer active", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        collector = SmartRecruitersCollector(client)
        result = collector.collect(target())

    assert result.status is CollectionStatus.SUCCESS
    assert len(result.jobs) == 1
    assert result.errors == []
    assert collector.last_counts["vanished"] == 1
    assert collector.last_counts["quarantined"] == 0


def test_smartrecruiters_later_page_rate_limit_status_survives_empty_hydration() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/companies/acme/postings":
            offset = int(request.url.params["offset"])
            if offset == 0:
                return httpx.Response(
                    200,
                    json={
                        "limit": 1,
                        "offset": 0,
                        "totalFound": 2,
                        "content": [posting("1")],
                    },
                    request=request,
                )
            return httpx.Response(429, text="Too many requests", request=request)
        pytest.fail("title filter should suppress hydration")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        collector = SmartRecruitersCollector(client, title_filter=lambda _title: False)
        collector.page_size = 1
        result = collector.collect(target())

    assert result.status is CollectionStatus.RATE_LIMITED
    assert result.jobs == []
    assert collector.last_counts["list_requests"] == 2
    assert collector.last_counts["detail_requests"] == 0
    assert result.errors == ["HTTP 429"]


@pytest.mark.parametrize("status_code", [405, 407, 408])
def test_smartrecruiters_unclassified_detail_4xx_is_target_wide(
    status_code: int,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/companies/acme/postings":
            return httpx.Response(
                200,
                json={
                    "limit": 100,
                    "offset": 0,
                    "totalFound": 2,
                    "content": [posting("1"), posting("2")],
                },
                request=request,
            )
        return httpx.Response(status_code, text="request-wide failure", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        collector = SmartRecruitersCollector(client)
        result = collector.collect(target())

    assert result.status is CollectionStatus.PROVIDER_ERROR
    assert result.jobs == []
    assert collector.last_counts["detail_requests"] == 1
    assert collector.last_counts["quarantined"] == 1
    assert result.errors == [f"detail[1] HTTP {status_code}"]


@pytest.mark.parametrize("status_code", [400, 422])
def test_smartrecruiters_posting_specific_4xx_is_quarantined_and_collection_continues(
    status_code: int,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/companies/acme/postings":
            return httpx.Response(
                200,
                json={
                    "limit": 100,
                    "offset": 0,
                    "totalFound": 2,
                    "content": [posting("1"), posting("2")],
                },
                request=request,
            )
        if request.url.path.endswith("/1"):
            return httpx.Response(status_code, text="posting-specific rejection", request=request)
        return httpx.Response(200, json=detail("2"), request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        collector = SmartRecruitersCollector(client)
        result = collector.collect(target())

    assert result.status is CollectionStatus.PARTIAL
    assert [job.source_job_id for job in result.jobs] == ["2"]
    assert collector.last_counts["detail_requests"] == 2
    assert collector.last_counts["quarantined"] == 1
    assert result.errors == [f"detail[1] HTTP {status_code}"]


def test_smartrecruiters_detail_410_is_vanished_and_collection_continues() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/companies/acme/postings":
            return httpx.Response(
                200,
                json={
                    "limit": 100,
                    "offset": 0,
                    "totalFound": 2,
                    "content": [posting("1"), posting("2")],
                },
                request=request,
            )
        if request.url.path.endswith("/1"):
            return httpx.Response(410, text="Gone", request=request)
        return httpx.Response(200, json=detail("2"), request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        collector = SmartRecruitersCollector(client)
        result = collector.collect(target())

    assert result.status is CollectionStatus.SUCCESS
    assert [job.source_job_id for job in result.jobs] == ["2"]
    assert collector.last_counts["detail_requests"] == 2
    assert collector.last_counts["vanished"] == 1
    assert collector.last_counts["quarantined"] == 0
    assert result.errors == []


def test_smartrecruiters_mismatched_detail_identity_is_quarantined() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/companies/acme/postings":
            return httpx.Response(
                200,
                json={
                    "limit": 100,
                    "offset": 0,
                    "totalFound": 1,
                    "content": [posting("1")],
                },
                request=request,
            )
        wrong = detail("2")
        return httpx.Response(200, json=wrong, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        collector = SmartRecruitersCollector(client)
        result = collector.collect(target())

    assert result.status is CollectionStatus.PARTIAL
    assert result.jobs == []
    assert collector.last_counts["quarantined"] == 1
    assert "does not match indexed posting" in result.errors[0]
