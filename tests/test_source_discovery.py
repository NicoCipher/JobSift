from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from job_scout.production_registry import (
    ProductionSourceRegistry,
    ProductionTarget,
)
from job_scout.source_discovery import (
    CommonCrawlDiscovery,
    DiscoveryQuery,
    SourceAdmissionProbe,
    candidate_from_url,
    overlay_admitted_targets,
)
from job_scout.storage.source_discovery import SourceDiscoveryStore
from job_scout.storage.sqlite import SQLiteRepository


NOW = datetime(2026, 10, 4, 0, 0, tzinfo=UTC)


def _base_registry() -> ProductionSourceRegistry:
    target = ProductionTarget(
        target_identity="greenhouse:known",
        source="greenhouse",
        coordinates={"board": "known"},
        company_hint="known",
        health_current_postings=3,
        health_inventory_exact=True,
    )
    return ProductionSourceRegistry(
        registry_id="base",
        target_universe_git_blob_sha="a" * 40,
        health_manifest_sha256="b" * 64,
        health_evidence_updated_at="2026-10-03T00:00:00Z",
        target_counts_by_source={"greenhouse": 1},
        targets=[target],
    )


def test_candidate_from_url_supports_all_production_provider_contracts() -> None:
    urls = {
        "greenhouse:acme": "https://job-boards.greenhouse.io/acme/jobs/123456",
        "greenhouse:legacy": "https://boards.greenhouse.io/legacy/jobs/123456",
        "ashby:acme": (
            "https://jobs.ashbyhq.com/acme/"
            "12345678-1234-1234-1234-123456789abc"
        ),
        "lever:global:acme": (
            "https://jobs.lever.co/acme/"
            "12345678-1234-1234-1234-123456789abc"
        ),
        "smartrecruiters:servicenow": (
            "https://jobs.smartrecruiters.com/ServiceNow/"
            "744000148862459-software-engineer"
        ),
        "workday:acme.wd1.myworkdayjobs.com:acme:External": (
            "https://acme.wd1.myworkdayjobs.com/en-US/External/job/"
            "Somewhere/Software-Engineer_R123"
        ),
    }
    for expected_identity, url in urls.items():
        candidate = candidate_from_url(url)
        assert candidate is not None
        assert candidate["target_identity"] == expected_identity

    assert candidate_from_url("https://example.com/jobs/123") is None


def test_discovery_store_requires_current_active_health_for_runtime_admission(
    tmp_path: Path,
) -> None:
    store = SourceDiscoveryStore(SQLiteRepository(tmp_path / "jobs.sqlite3"))
    candidate = candidate_from_url(
        "https://jobs.smartrecruiters.com/ServiceNow/"
        "744000148862459-software-engineer"
    )
    assert candidate is not None

    inserted, evidence = store.observe_candidate(
        **candidate,
        discovery_source="commoncrawl-cdx",
        crawl_id="CC-MAIN-2026-39",
        query_id="smartrecruiters",
        captured_at="20261003120000",
        discovered_url=(
            "https://jobs.smartrecruiters.com/ServiceNow/"
            "744000148862459-software-engineer"
        ),
        observed_at=NOW,
    )
    assert inserted is True
    assert evidence is True

    duplicate = store.observe_candidate(
        **candidate,
        discovery_source="commoncrawl-cdx",
        crawl_id="CC-MAIN-2026-39",
        query_id="smartrecruiters",
        captured_at="20261003120000",
        discovered_url=(
            "https://jobs.smartrecruiters.com/ServiceNow/"
            "744000148862459-software-engineer"
        ),
        observed_at=NOW + timedelta(minutes=1),
    )
    assert duplicate == (False, False)
    assert store.admitted_targets(now=NOW) == []

    store.record_health(
        target_identity=candidate["target_identity"],
        checked_at=NOW,
        result={
            "classification": "active",
            "request_count": 1,
            "http_status": 200,
            "current_postings": 707,
            "inventory_exact": True,
            "error": None,
        },
    )
    assert len(store.admitted_targets(now=NOW + timedelta(hours=47))) == 1
    assert store.admitted_targets(now=NOW + timedelta(hours=49)) == []

    store.record_health(
        target_identity=candidate["target_identity"],
        checked_at=NOW + timedelta(days=1),
        result={
            "classification": "valid_empty",
            "request_count": 1,
            "http_status": 200,
            "current_postings": 0,
            "inventory_exact": True,
            "error": None,
        },
    )
    assert store.admitted_targets(now=NOW + timedelta(days=1)) == []


def test_runtime_overlay_dedupes_frozen_targets_and_hashes_dynamic_admission() -> None:
    base = _base_registry()
    dynamic = ProductionTarget(
        target_identity="smartrecruiters:servicenow",
        source="smartrecruiters",
        coordinates={"board": "servicenow"},
        company_hint="servicenow",
        health_current_postings=707,
        health_inventory_exact=True,
    )
    duplicate = base.targets[0].model_copy()

    runtime, evidence = overlay_admitted_targets(base, [dynamic, duplicate])

    assert runtime.registry_id.startswith("base-live-")
    assert len(runtime.targets) == 2
    assert runtime.target_counts_by_source == {
        "greenhouse": 1,
        "smartrecruiters": 1,
    }
    assert evidence["dynamic_admitted_targets"] == 1
    assert len(evidence["dynamic_admission_sha256"]) == 64
    assert evidence["runtime_registry_sha256"]


def test_common_crawl_discovery_advances_persisted_page_cursor(tmp_path: Path) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/collinfo.json":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "CC-MAIN-2026-39",
                        "cdx-api": (
                            "https://index.commoncrawl.org/"
                            "CC-MAIN-2026-39-index"
                        ),
                    }
                ],
                request=request,
            )
        params = request.url.params
        if params.get("showNumPages") == "true":
            return httpx.Response(
                200,
                json={"blocks": 2, "pages": 2, "pageSize": 1},
                request=request,
            )
        page = int(params.get("page", "0"))
        posting_id = "744000148862459" if page == 0 else "744000148862460"
        body = json.dumps(
            {
                "url": (
                    "https://jobs.smartrecruiters.com/ServiceNow/"
                    f"{posting_id}-software-engineer"
                ),
                "timestamp": "20261003120000",
                "status": "200",
                "mime": "text/html",
            }
        )
        return httpx.Response(200, text=body + "\n", request=request)

    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")
    store = SourceDiscoveryStore(repository)
    query = DiscoveryQuery("smartrecruiters", "jobs.smartrecruiters.com/*")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        discovery = CommonCrawlDiscovery(
            client,
            store,
            delay_seconds=0,
            queries=(query,),
        )
        first = discovery.discover(
            base_target_identities=set(),
            observed_at=NOW,
        )
        second = discovery.discover(
            base_target_identities=set(),
            observed_at=NOW + timedelta(hours=1),
        )

    assert first["new_targets"] == 1
    assert second["new_targets"] == 0
    cursor = store.get_cursor(
        discovery_source="commoncrawl-cdx",
        query_id="smartrecruiters",
    )
    assert cursor is not None
    assert cursor["page"] == CommonCrawlDiscovery._initial_page(
        crawl_id="CC-MAIN-2026-39",
        query_id="smartrecruiters",
        pages=2,
    )
    assert cursor["pages"] == 2
    # Page count is fetched only for the first encounter with this crawl.
    assert sum("showNumPages=true" in value for value in requests) == 1
    assert any("page=0" in value for value in requests)
    assert any("page=1" in value for value in requests)


def test_provider_health_probe_is_authoritative_for_smartrecruiters() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.smartrecruiters.com"
        return httpx.Response(
            200,
            json={
                "offset": 0,
                "limit": 1,
                "totalFound": 23,
                "content": [{"id": "posting"}],
            },
            request=request,
        )

    target = {
        "target_identity": "smartrecruiters:servicenow",
        "source": "smartrecruiters",
        "coordinates": {"board": "servicenow"},
        "company_hint": "servicenow",
    }
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = SourceAdmissionProbe(
            client,
            delay_seconds=0,
            retries=0,
        ).probe(target)

    assert result == {
        "classification": "active",
        "request_count": 1,
        "http_status": 200,
        "current_postings": 23,
        "inventory_exact": True,
        "error": None,
    }


def test_provider_health_probe_never_treats_404_as_empty_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="missing", request=request)

    target = {
        "target_identity": "ashby:missing",
        "source": "ashby",
        "coordinates": {"board": "missing"},
        "company_hint": "missing",
    }
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = SourceAdmissionProbe(
            client,
            delay_seconds=0,
            retries=0,
        ).probe(target)

    assert result["classification"] == "invalid"
    assert result["current_postings"] is None
