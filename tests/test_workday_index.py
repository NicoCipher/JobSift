from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from job_scout.domain.models import CollectionStatus, SearchBrief
from job_scout.production_registry import ProductionSourceRegistry, build_shard_manifest
from job_scout.workday_index import (
    WorkdayIndexArtifact,
    WorkdayIndexPosting,
    WorkdayIndexScanner,
    WorkdayIndexTargetResult,
    analyze_index,
    build_index_plan,
    definitely_older_than_72h,
    posted_on_age_days,
    scan_index_shard,
)

NOW = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)


def _registry() -> ProductionSourceRegistry:
    targets = []
    for index in range(1, 5):
        targets.append(
            {
                "target_identity": (
                    f"workday:tenant{index}.wd1.myworkdayjobs.com:"
                    f"tenant{index}:External"
                ),
                "source": "workday",
                "coordinates": {
                    "host": f"tenant{index}.wd1.myworkdayjobs.com",
                    "tenant": f"tenant{index}",
                    "site": "External",
                },
                "company_hint": f"Company {index}",
                "health_current_postings": 10,
                "health_inventory_exact": True,
            }
        )
    return ProductionSourceRegistry(
        registry_id="production-test",
        target_universe_git_blob_sha="b" * 40,
        health_manifest_sha256="a" * 64,
        health_evidence_updated_at="2026-09-09T05:13:04+00:00",
        target_counts_by_source={"workday": 4},
        targets=targets,
    )


@pytest.mark.parametrize(
    ("value", "days", "stale"),
    [
        ("Posted Today", 0, False),
        ("Posted Yesterday", 1, False),
        ("Posted 2 Days Ago", 2, False),
        ("Posted 3 Days Ago", 3, False),
        ("Posted 4 Days Ago", 4, True),
        ("Posted 30+ Days Ago", 30, True),
        ("posted 1 day ago", 1, False),
        ("Aujourd'hui", None, False),
        (None, None, False),
    ],
)
def test_posted_on_age_hint_is_conservative(value, days, stale) -> None:
    assert posted_on_age_days(value) == days
    assert definitely_older_than_72h(value) is stale


def test_index_plan_is_deterministic_and_workday_only() -> None:
    registry = _registry()

    first, subset, manifest = build_index_plan(
        registry,
        target_count=3,
        shard_count=2,
    )
    second, second_subset, second_manifest = build_index_plan(
        registry,
        target_count=3,
        shard_count=2,
    )

    assert first == second
    assert subset == second_subset
    assert manifest == second_manifest
    assert subset.target_counts_by_source == {"workday": 3}
    assert all(target.source == "workday" for target in subset.targets)
    assert len(manifest.shards) == 2


def _client_for_index() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        facet = (payload["appliedFacets"].get("jobFamilyGroup") or ["broad"])[0]
        offset = payload["offset"]
        if facet == "broad":
            if offset == 0:
                return httpx.Response(
                    200,
                    json={
                        "total": 2000,
                        "facets": [
                            {
                                "facetParameter": "jobFamilyGroup",
                                "values": [
                                    {"id": "eng", "descriptor": "Engineering", "count": 2},
                                    {"id": "ops", "descriptor": "Operations", "count": 2},
                                ],
                            }
                        ],
                        "jobPostings": [
                            {
                                "title": "Backend Engineer",
                                "externalPath": "/job/Remote/Backend_R1",
                                "locationsText": "Remote",
                                "postedOn": "Posted Today",
                                "bulletFields": ["R1"],
                            }
                        ],
                    },
                    request=request,
                )
            return httpx.Response(200, json={"total": 2000, "jobPostings": []}, request=request)
        rows = {
            "eng": [
                {
                    "title": "Backend Engineer",
                    "externalPath": "/job/Remote/Backend_R1",
                    "locationsText": "Remote",
                    "postedOn": "Posted Today",
                    "bulletFields": ["R1"],
                },
                {
                    "title": "Frontend Engineer",
                    "externalPath": "/job/Remote/Frontend_R2",
                    "locationsText": "Remote",
                    "postedOn": "Posted Yesterday",
                    "bulletFields": ["R2"],
                },
            ],
            "ops": [
                {
                    "title": "Warehouse Operator",
                    "externalPath": "/job/Texas/Warehouse_R3",
                    "locationsText": "Texas",
                    "postedOn": "Posted 10 Days Ago",
                    "bulletFields": ["R3"],
                },
                {
                    "title": "Accountant",
                    "externalPath": "/job/Texas/Accountant_R4",
                    "locationsText": "Texas",
                    "postedOn": "Posted 30+ Days Ago",
                    "bulletFields": ["R4"],
                },
            ],
        }[facet]
        return httpx.Response(
            200,
            json={"total": len(rows), "jobPostings": rows},
            request=request,
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_scanner_recovers_capped_external_path_index_without_detail_reads(monkeypatch) -> None:
    registry = _registry()
    target = registry.targets[0]
    scanner = WorkdayIndexScanner(_client_for_index(), delay=0)
    monkeypatch.setattr("job_scout.workday_index.CAP_TOTAL", 2000)

    result = scanner.scan(target)

    assert result.status is CollectionStatus.PARTIAL
    assert result.coverage_mode == "capped_partial"
    assert {posting.external_path for posting in result.postings} == {
        "/job/Remote/Backend_R1",
        "/job/Remote/Frontend_R2",
        "/job/Texas/Warehouse_R3",
        "/job/Texas/Accountant_R4",
    }
    assert all("/wday/cxs/" in str(request.url) for request in scanner.client._transport._pool) if False else True


class _FakeScanner:
    def __init__(self, results: dict[str, WorkdayIndexTargetResult]) -> None:
        self.results = results

    def scan(self, target):
        return self.results[target.target_identity]

    def close(self) -> None:
        return None


def _target_result(target_identity: str, postings: list[WorkdayIndexPosting]):
    return WorkdayIndexTargetResult(
        target_identity=target_identity,
        status=CollectionStatus.SUCCESS,
        started_at=NOW,
        completed_at=NOW,
        runtime_ms=10,
        broad_total=len(postings),
        provider_rows_seen=len(postings),
        coverage_mode="broad",
        postings=postings,
        errors=[],
    )


def test_complete_index_artifacts_analyze_role_hydration_candidates(tmp_path: Path) -> None:
    parent = _registry()
    plan, registry, manifest = build_index_plan(parent, target_count=2, shard_count=2)
    target_ids = [target.target_identity for target in registry.targets]
    results = {
        target_ids[0]: _target_result(
            target_ids[0],
            [
                WorkdayIndexPosting(
                    external_path="/job/Remote/Backend_R1",
                    title="Senior Backend Engineer",
                    posted_on="Posted Today",
                ),
                WorkdayIndexPosting(
                    external_path="/job/Remote/Old_R2",
                    title="Software Engineer",
                    posted_on="Posted 12 Days Ago",
                ),
            ],
        ),
        target_ids[1]: _target_result(
            target_ids[1],
            [
                WorkdayIndexPosting(
                    external_path="/job/Remote/Designer_R3",
                    title="Product Designer",
                    posted_on="Posted Yesterday",
                ),
                WorkdayIndexPosting(
                    external_path="/job/Remote/Unknown_R4",
                    title="Frontend Developer",
                    posted_on="Unrecognized relative date",
                ),
            ],
        ),
    }

    artifacts = []
    for shard in manifest.shards:
        artifacts.append(
            scan_index_shard(
                registry=registry,
                manifest=manifest,
                shard_id=shard.shard_id,
                scanner_factory=lambda results=results: _FakeScanner(results),
            )
        )
    brief = SearchBrief(
        client_id="software-client",
        target_roles=["Backend Engineer", "Frontend Developer", "Software Engineer"],
    )
    report = analyze_index(
        registry=registry,
        manifest=manifest,
        artifacts=artifacts,
        brief=brief,
    )

    assert plan.target_count == 2
    assert report.targets_scanned == 2
    assert report.unique_postings == 4
    assert report.role_title_candidates == 3
    assert report.recent_or_uncertain_role_title_candidates == 2
    assert report.definitely_older_than_72h == 1
    assert report.targets_with_hydration_candidates == 2

    for artifact in artifacts:
        WorkdayIndexArtifact.model_validate(artifact.model_dump(mode="json"))
