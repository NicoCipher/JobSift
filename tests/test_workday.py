from __future__ import annotations

import json
import uuid
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from job_scout.collectors import workday
from job_scout.collectors.workday import WorkdayCollector
from job_scout.domain.models import (
    CollectionStatus,
    RemoteStatus,
    SourceTarget,
    WorkdayTargetConfig,
)

CONFIG = WorkdayTargetConfig(host="acme.wd1.myworkdayjobs.com", tenant="acme", site="External")
TARGET = SourceTarget(board_id=CONFIG.board_id, company="Acme", workday=CONFIG)
LIVE_DETAIL = json.loads(
    (Path(__file__).parent / "fixtures/workday_detail_bigcommerce.json").read_text()
)


def posting(path: str) -> dict[str, str]:
    return {"externalPath": path}


def detail(path: str, job_id: str | None = None) -> dict:
    return {
        "jobPostingInfo": {
            "jobReqId": job_id or path.rsplit("_", 1)[-1],
            "title": "Technical Support Engineer",
            "hiringOrganization": {"descriptor": "Acme Holdings"},
            "jobDescription": "<p>Investigate customer incidents.</p>",
            "externalPath": path,
            "externalUrl": f"https://{CONFIG.host}/en-US/{CONFIG.site}{path}?source=board",
            "location": "Austin, TX, United States",
            "additionalLocations": [{"descriptor": "Remote, United States"}],
            "country": {"descriptor": "United States of America"},
            "remoteType": "Remote",
            "timeType": "Full time",
            "startDate": "2026-09-05",
            "posted": True,
            "postedOn": "Posted Today",
        }
    }


def client_for_pages(pages: dict[tuple[str, int], dict], details: dict[str, dict]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            payload = json.loads(request.content)
            group = (payload["appliedFacets"].get("jobFamilyGroup") or ["broad"])[0]
            return httpx.Response(200, json=pages[(group, payload["offset"])], request=request)
        path = request.url.path.split(CONFIG.site, 1)[-1]
        return httpx.Response(200, json=details[path], request=request)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_normal_pagination_normalizes_cxs_evidence() -> None:
    first = "/job/Austin-TX/Support-Engineer_R1"
    second = "/job/Remote/Support-Engineer_R2"
    pages = {
        ("broad", 0): {"total": 21, "facets": [], "jobPostings": [posting(first)] * 20},
        ("broad", 20): {"total": 21, "jobPostings": [posting(second)]},
    }
    # The duplicate first-page rows are deliberately a provider inconsistency;
    # provider-ID union retains one row and collection stays partial below.
    result = WorkdayCollector(
        client_for_pages(pages, {first: detail(first), second: detail(second)}), delay=0
    ).collect(TARGET)

    assert result.status is CollectionStatus.PARTIAL
    assert len(result.jobs) == 2
    job = next(job for job in result.jobs if job.source_job_id == "R1")
    assert job.source_board_id == CONFIG.board_id
    assert job.company == "Acme Holdings"
    assert job.id == str(uuid.uuid5(uuid.NAMESPACE_URL, f"workday:{CONFIG.board_id}:R1"))
    assert str(job.job_url).endswith("/Support-Engineer_R1")
    assert job.apply_url is None
    assert job.description_text == "Investigate customer incidents."
    assert job.country == "United States"
    assert job.remote_status is RemoteStatus.REMOTE
    assert job.posted_at and job.posted_at.isoformat() == "2026-09-05T00:00:00+00:00"
    assert job.updated_at is None


@pytest.mark.parametrize(
    ("overrides", "provider_id", "remote", "employment"),
    [
        ({}, "3100", RemoteStatus.REMOTE, "full_time"),
        (
            {"location": "Pittsburgh, PA, United States", "remoteType": None},
            "JR102834",
            RemoteStatus.UNKNOWN,
            "full_time",
        ),
        (
            {"location": "Remote - Nationwide", "country": None, "remoteType": "Remote"},
            "R-100673",
            RemoteStatus.REMOTE,
            "full_time",
        ),
        (
            {
                "location": "Toronto, Canada",
                "country": {"descriptor": "Canada"},
                "remoteType": "Hybrid",
                "timeType": "Part time",
            },
            "INT-1",
            RemoteStatus.HYBRID,
            "part_time",
        ),
        (
            {
                "location": "Austin, TX, United States",
                "additionalLocations": [
                    {"descriptor": "New York, NY, United States"},
                    {"descriptor": "Remote, United States"},
                ],
                "remoteType": "In-Office",
            },
            "MULTI-1",
            RemoteStatus.ONSITE,
            "full_time",
        ),
    ],
)
def test_live_detail_shapes_without_external_path_normalize(
    overrides, provider_id, remote, employment
) -> None:
    body = deepcopy(LIVE_DETAIL)
    info = body["jobPostingInfo"]
    info.update(overrides)
    info["jobReqId"] = provider_id
    path = "/job/United-States---Remote/Technical-Support-Representative--US-_3100"

    job = WorkdayCollector(delay=0)._normalize(body, TARGET, CONFIG, path)

    assert job.source_job_id == provider_id
    assert job.source_board_id == CONFIG.board_id
    assert str(job.job_url) == info["externalUrl"]
    assert job.apply_url is None and job.updated_at is None
    assert job.remote_status is remote
    assert job.employment_type and job.employment_type.value == employment
    assert job.posted_at and job.posted_at.isoformat() == "2025-11-19T00:00:00+00:00"


def test_conflicting_detail_external_path_is_still_quarantined() -> None:
    body = deepcopy(LIVE_DETAIL)
    body["jobPostingInfo"]["externalPath"] = "/job/other/R1"

    with pytest.raises(ValueError, match="externalPath"):
        WorkdayCollector(delay=0)._normalize(body, TARGET, CONFIG, "/job/expected/R1")


def test_detail_read_timeout_retries_then_normalizes() -> None:
    path = "/job/Remote/Support_R1"
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.method == "POST":
            return httpx.Response(
                200, json={"total": 1, "facets": [], "jobPostings": [posting(path)]}
            )
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("temporary read timeout", request=request)
        return httpx.Response(200, json=detail(path), request=request)

    result = WorkdayCollector(
        httpx.Client(transport=httpx.MockTransport(handler)), delay=0
    ).collect(TARGET)

    assert result.status is CollectionStatus.SUCCESS
    assert [job.source_job_id for job in result.jobs] == ["R1"]
    assert attempts == 2


def test_exhausted_detail_timeouts_are_partial_and_later_jobs_continue() -> None:
    failed = "/job/Remote/Support_R1"
    healthy = "/job/Remote/Support_R2"
    attempts: dict[str, int] = {failed: 0, healthy: 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"total": 2, "facets": [], "jobPostings": [posting(failed), posting(healthy)]},
            )
        path = request.url.path.split(CONFIG.site, 1)[-1]
        attempts[path] += 1
        if path == failed:
            raise httpx.ReadTimeout("persistent read timeout", request=request)
        return httpx.Response(200, json=detail(path), request=request)

    result = WorkdayCollector(
        httpx.Client(transport=httpx.MockTransport(handler)), delay=0
    ).collect(TARGET)

    assert result.status is CollectionStatus.PARTIAL
    assert [job.source_job_id for job in result.jobs] == ["R2"]
    assert attempts == {failed: 3, healthy: 1}
    assert any("network ReadTimeout" in error for error in result.errors)


def test_permanent_http_failures_keep_existing_semantics() -> None:
    invalid = WorkdayCollector(
        httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(404, request=request))
        ),
        delay=0,
    ).collect(TARGET)
    assert invalid.status is CollectionStatus.INVALID_TARGET

    failed = "/job/Remote/Support_R1"
    healthy = "/job/Remote/Support_R2"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"total": 2, "facets": [], "jobPostings": [posting(failed), posting(healthy)]},
            )
        path = request.url.path.split(CONFIG.site, 1)[-1]
        return (
            httpx.Response(404, request=request)
            if path == failed
            else httpx.Response(200, json=detail(path), request=request)
        )

    result = WorkdayCollector(
        httpx.Client(transport=httpx.MockTransport(handler)), delay=0
    ).collect(TARGET)
    assert result.status is CollectionStatus.PARTIAL
    assert [job.source_job_id for job in result.jobs] == ["R2"]
    assert f"detail {failed}: HTTP 404" in result.errors


@pytest.mark.parametrize("exception_type", [httpx.ConnectError, httpx.ReadTimeout])
def test_exhausted_broad_network_failures_are_not_parse_failures(
    exception_type: type[Exception],
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise exception_type("transient transport failure", request=request)

    result = WorkdayCollector(
        httpx.Client(transport=httpx.MockTransport(handler)), delay=0
    ).collect(TARGET)

    assert attempts == workday.RETRIES + 1
    assert result.status is CollectionStatus.NETWORK_FAILURE
    assert result.errors == [
        f"broad offset 0: network {exception_type.__name__}: transient transport failure"
    ]


@pytest.mark.parametrize(
    ("status_code", "expected_status", "expected_attempts"),
    [
        (429, CollectionStatus.RATE_LIMITED, workday.RETRIES + 1),
        (500, CollectionStatus.PROVIDER_ERROR, workday.RETRIES + 1),
        (404, CollectionStatus.INVALID_TARGET, 1),
    ],
)
def test_broad_http_failures_keep_terminal_status_semantics(
    status_code: int, expected_status: CollectionStatus, expected_attempts: int
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status_code, request=request)

    result = WorkdayCollector(
        httpx.Client(transport=httpx.MockTransport(handler)), delay=0
    ).collect(TARGET)

    assert attempts == expected_attempts
    assert result.status is expected_status
    assert result.errors == [f"broad offset 0: HTTP {status_code}"]


def test_broad_non_json_response_is_a_parse_failure() -> None:
    result = WorkdayCollector(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"not json", request=request)
            )
        ),
        delay=0,
    ).collect(TARGET)

    assert result.status is CollectionStatus.PARSE_FAILURE
    assert result.errors == ["broad offset 0: response is not JSON"]


@pytest.mark.parametrize("body", [{"jobPostings": []}, {"total": "1", "jobPostings": []}])
def test_broad_missing_or_invalid_total_is_a_parse_failure(body: dict) -> None:
    result = WorkdayCollector(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=body, request=request)
            )
        ),
        delay=0,
    ).collect(TARGET)

    assert result.status is CollectionStatus.PARSE_FAILURE
    assert result.errors == ["broad offset 0: response has no non-negative integer total"]


def test_capped_board_recovers_safe_job_family_group_union(monkeypatch) -> None:
    monkeypatch.setattr(workday, "CAP_TOTAL", 3)
    first = "/job/A/Support_R1"
    second = "/job/B/Support_R2"
    third = "/job/C/Support_R3"
    fourth = "/job/D/Support_R4"
    pages = {
        ("broad", 0): {
            "total": 3,
            "jobPostings": [posting(first), posting(second), posting(third)],
            "facets": [
                {
                    "facetParameter": "jobFamilyGroup",
                    "values": [
                        {"id": "engineering", "descriptor": "Engineering", "count": 2},
                        {"id": "sales", "descriptor": "Sales", "count": 2},
                    ],
                }
            ],
        },
        ("engineering", 0): {"total": 2, "jobPostings": [posting(first), posting(second)]},
        ("sales", 0): {"total": 2, "jobPostings": [posting(third), posting(fourth)]},
    }
    details = {path: detail(path) for path in (first, second, third, fourth)}
    collector = WorkdayCollector(client_for_pages(pages, details), delay=0)

    result = collector.collect(TARGET)

    assert result.status is CollectionStatus.SUCCESS
    assert {job.source_job_id for job in result.jobs} == {"R1", "R2", "R3", "R4"}
    assert collector.last_counts["coverage_mode"] == "job_family_group_partition"
    assert collector.last_counts["paths_discovered"] == 4


def test_capped_board_without_safe_partition_contract_is_partial(monkeypatch) -> None:
    monkeypatch.setattr(workday, "CAP_TOTAL", 3)
    paths = [f"/job/A/Support_R{number}" for number in range(1, 4)]
    pages = {
        ("broad", 0): {
            "total": 3,
            "jobPostings": [posting(path) for path in paths],
            "facets": [],
        }
    }
    result = WorkdayCollector(
        client_for_pages(pages, {path: detail(path) for path in paths}), delay=0
    ).collect(TARGET)

    assert result.status is CollectionStatus.PARTIAL
    assert len(result.jobs) == 3
    assert "no safe jobFamilyGroup" in result.errors[0]


def test_partition_provider_id_overlap_is_partial(monkeypatch) -> None:
    monkeypatch.setattr(workday, "CAP_TOTAL", 3)
    first = "/job/A/Support_R1"
    second = "/job/B/Support_R2"
    third = "/job/C/Support_R3"
    pages = {
        ("broad", 0): {
            "total": 3,
            "jobPostings": [posting(first), posting(second), posting(third)],
            "facets": [
                {
                    "facetParameter": "jobFamilyGroup",
                    "values": [
                        {"id": "one", "descriptor": "One", "count": 2},
                        {"id": "two", "descriptor": "Two", "count": 2},
                    ],
                }
            ],
        },
        ("one", 0): {"total": 2, "jobPostings": [posting(first), posting(second)]},
        ("two", 0): {"total": 2, "jobPostings": [posting(second), posting(third)]},
    }
    result = WorkdayCollector(
        client_for_pages(
            pages,
            {
                first: detail(first),
                second: detail(second),
                third: detail(third, "R2"),
            },
        ),
        delay=0,
    ).collect(TARGET)

    assert result.status is CollectionStatus.PARTIAL
    assert {job.source_job_id for job in result.jobs} == {"R1", "R2"}
    assert any("overlap by provider jobReqId" in error for error in result.errors)


def test_repeated_page_signature_stops_before_cap_boundary() -> None:
    paths = [f"/job/A/Support_R{number}" for number in range(20)]
    pages = {
        ("broad", 0): {"total": 21, "facets": [], "jobPostings": [posting(path) for path in paths]},
        ("broad", 20): {"total": 21, "jobPostings": [posting(path) for path in paths]},
    }
    result = WorkdayCollector(
        client_for_pages(pages, {path: detail(path) for path in paths}), delay=0
    ).collect(TARGET)

    assert result.status is CollectionStatus.PARTIAL
    assert any("repeated page signature" in error for error in result.errors)


@pytest.mark.parametrize(
    ("target", "message"),
    [
        (SourceTarget(board_id="acme", company="Acme"), "requires explicit"),
        (
            SourceTarget(board_id="wrong", company="Acme", workday=CONFIG),
            "board_id must equal",
        ),
    ],
)
def test_explicit_target_configuration_is_required(target: SourceTarget, message: str) -> None:
    result = WorkdayCollector(delay=0).collect(target)

    assert result.status is CollectionStatus.INVALID_TARGET
    assert message in result.errors[0]
