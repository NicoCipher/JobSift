from __future__ import annotations

import json
import uuid

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
