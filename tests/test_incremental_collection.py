from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from job_scout.collectors.greenhouse import GreenhouseCollector
from job_scout.collectors.smartrecruiters import SmartRecruitersCollector
from job_scout.domain.models import CollectionStatus, Job, SourceTarget
from job_scout.incremental_collection import (
    IncrementalTargetState,
    snapshot_incremental_target_state,
)
from job_scout.production_registry import ProductionSourceRegistry, ProductionTarget
from job_scout.storage.sqlite import SQLiteRepository


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


def test_greenhouse_vanished_rows_do_not_exhaust_detail_budget() -> None:
    vanished = [greenhouse_job(job_id) for job_id in range(1, 201)]
    fresh = greenhouse_job(201)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/boards/acme/jobs":
            return httpx.Response(
                200,
                json={"jobs": [*vanished, fresh]},
                request=request,
            )
        job_id = int(request.url.path.rsplit("/", 1)[-1])
        if job_id <= 200:
            return httpx.Response(404, request=request)
        assert job_id == 201
        return httpx.Response(
            200,
            json={
                **greenhouse_job(201),
                "first_published": datetime.now(UTC).isoformat(),
            },
            request=request,
        )

    state = IncrementalTargetState(source="greenhouse", board_id="acme")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        collector = GreenhouseCollector(
            client,
            incremental_states=[state],
            incremental_detail_limit=200,
        )
        result = collector.collect(target())

    assert result.status is CollectionStatus.SUCCESS
    assert [job.source_job_id for job in result.jobs] == ["201"]
    assert collector.last_counts["vanished"] == 200
    assert collector.last_counts["detail_requests"] == 201
    assert collector.last_counts["detail_budget_used"] == 1
    assert collector.last_counts["detail_deferred"] == 0


def test_greenhouse_quarantined_details_make_bounded_progress() -> None:
    indexed = [greenhouse_job(job_id) for job_id in range(1, 202)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/boards/acme/jobs":
            return httpx.Response(
                200,
                json={"jobs": indexed},
                request=request,
            )
        job_id = int(request.url.path.rsplit("/", 1)[-1])
        if job_id <= 200:
            return httpx.Response(
                200,
                json={"id": job_id, "title": None},
                request=request,
            )
        assert job_id == 201
        return httpx.Response(
            200,
            json={
                **greenhouse_job(201),
                "first_published": datetime.now(UTC).isoformat(),
            },
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = GreenhouseCollector(
            client,
            incremental_states=[
                IncrementalTargetState(source="greenhouse", board_id="acme")
            ],
            incremental_detail_limit=200,
        )
        first_result = first.collect(target())

        assert first_result.status is CollectionStatus.PARTIAL
        assert len(first_result.jobs) == 200
        assert all(job.posted_at is None for job in first_result.jobs)
        assert first.last_counts["quarantined"] == 200
        assert first.last_counts["detail_deferred"] == 1

        retry_state = IncrementalTargetState(
            source="greenhouse",
            board_id="acme",
            known_source_job_ids=sorted(
                job.source_job_id for job in first_result.jobs
            ),
        )
        second = GreenhouseCollector(
            client,
            incremental_states=[retry_state],
            incremental_detail_limit=200,
        )
        second_result = second.collect(target())

    assert second_result.status is CollectionStatus.SUCCESS
    assert [job.source_job_id for job in second_result.jobs] == ["201"]
    assert second.last_counts["suppressed_known_unknown_age"] == 200
    assert second.last_counts["detail_requests"] == 1
    assert second.last_counts["detail_deferred"] == 0


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


def test_snapshot_includes_live_job_identity_and_posted_at_without_retention_evidence(
    tmp_path,
) -> None:
    repository = SQLiteRepository(tmp_path / "inventory.db")
    posted_at = datetime.now(UTC)
    repository.upsert_job(
        Job(
            id="job-1",
            source="greenhouse",
            source_job_id="1",
            source_board_id="acme",
            title="Software Engineer",
            company="Acme Inc",
            job_url="https://boards.greenhouse.io/acme/jobs/1",
            canonical_url="https://boards.greenhouse.io/acme/jobs/1",
            posted_at=posted_at,
            content_fingerprint="fingerprint-1",
        )
    )
    with repository.connect() as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM job_retention_evidence WHERE job_id=?",
                ("job-1",),
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT 1 FROM job_identity_ledger "
                "WHERE source=? AND source_board_id=? AND source_job_id=?",
                ("greenhouse", "acme", "1"),
            ).fetchone()
            is None
        )

    registry = ProductionSourceRegistry(
        registry_id="incremental-test",
        target_universe_git_blob_sha="0" * 40,
        health_manifest_sha256="1" * 64,
        health_evidence_updated_at="2026-10-04T00:00:00Z",
        target_counts_by_source={"greenhouse": 1},
        targets=[
            ProductionTarget(
                target_identity="greenhouse:acme",
                source="greenhouse",
                coordinates={"board": "acme"},
                company_hint="Acme Inc",
            )
        ],
    )

    states = snapshot_incremental_target_state(repository, registry)

    assert len(states) == 1
    assert states[0].known_source_job_ids == ["1"]
    assert states[0].active_posted_at_by_source_job_id == {"1": posted_at}
