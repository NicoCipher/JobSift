from datetime import UTC, datetime

import httpx
import pytest

from job_scout.collectors.smartrecruiters import SmartRecruitersCollector
from job_scout.domain.models import CollectionStatus, SourceTarget
from job_scout.sourcing_plan import SmartRecruitersPlanTarget
from tests.test_smartrecruiters import detail, posting, target
from validation.smartrecruiters_provider_v1.run import canonical_posting_url, valid_apply_url


def collect(handler, **kwargs):
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        collector = SmartRecruitersCollector(client, **kwargs)
        result = collector.collect(target())
        return collector, result


def index(request, items):
    return httpx.Response(
        200,
        json={"limit": 100, "offset": 0, "totalFound": len(items), "content": items},
        request=request,
    )


@pytest.mark.parametrize(
    "status,expected",
    [
        (429, CollectionStatus.RATE_LIMITED),
        (401, CollectionStatus.AUTHENTICATION_FAILURE),
        (403, CollectionStatus.FORBIDDEN),
        (503, CollectionStatus.PROVIDER_ERROR),
    ],
)
@pytest.mark.parametrize("valid_first", [False, True])
def test_detail_outage_stops_target(status, expected, valid_first):
    def handler(request):
        if request.url.path.endswith("/postings"):
            return index(request, [posting(str(i)) for i in range(1, 4)])
        if valid_first and request.url.path.endswith("/1"):
            return httpx.Response(200, json=detail("1"), request=request)
        return httpx.Response(status, request=request)

    collector, result = collect(handler)
    assert result.status == (CollectionStatus.PARTIAL if valid_first else expected)
    assert collector.last_counts["detail_requests"] == (2 if valid_first else 1)
    assert collector.last_counts["quarantined"] == 1


@pytest.mark.parametrize(
    "error",
    [httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.DecodingError, httpx.TooManyRedirects],
)
def test_transport_failure_is_isolated(error):
    def handler(request):
        raise error("failed", request=request)

    _, result = collect(handler)
    assert result.status == CollectionStatus.NETWORK_FAILURE


def test_empty_board_needs_no_hydration():
    collector, result = collect(lambda request: index(request, []))
    assert result.status == CollectionStatus.SUCCESS
    assert result.jobs == []
    assert collector.last_counts["detail_requests"] == 0


@pytest.mark.parametrize(
    "country,expected",
    [
        ("de", "Germany"),
        ("fr", "France"),
        ("ng", "Nigeria"),
        ("United States", "United States"),
        ("zz", None),
    ],
)
def test_countries_are_names_or_unknown(country, expected):
    def handler(request):
        if request.url.path.endswith("/postings"):
            return index(request, [posting("1")])
        body = detail("1")
        body["location"]["country"] = country
        return httpx.Response(200, json=body, request=request)

    _, result = collect(handler)
    assert result.jobs[0].country == expected
    assert result.jobs[0].eligible_countries == ({expected} if expected else set())


@pytest.mark.parametrize("board", ["", "a/b", "../x", "a?query", "a b", "a#fragment"])
def test_bad_coordinates_are_rejected(board):
    with pytest.raises(ValueError):
        SmartRecruitersPlanTarget(source="smartrecruiters", board=board, company="Acme")
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("no request"))) as client:
        result = SmartRecruitersCollector(client).collect(
            SourceTarget(board_id=board, company="Acme")
        )
    assert result.status == CollectionStatus.INVALID_TARGET


def test_duplicate_index_identity_hydrates_once_and_is_stable():
    def handler(request):
        if request.url.path.endswith("/postings"):
            return index(request, [posting("1"), posting("1")])
        return httpx.Response(200, json=detail("1"), request=request)

    first, result = collect(handler)
    _, again = collect(handler)
    assert len(result.jobs) == 1
    assert result.jobs[0].id == again.jobs[0].id
    assert first.last_counts["detail_requests"] == 1
    assert result.jobs[0].posted_at == datetime(2026, 10, 1, 12, tzinfo=UTC)


def test_malformed_index_counts_quarantine():
    def handler(request):
        if request.url.path.endswith("/postings"):
            return index(request, [{"bad": True}, posting("1")])
        return httpx.Response(200, json=detail("1"), request=request)

    collector, result = collect(handler)
    assert result.status == CollectionStatus.PARTIAL
    assert collector.last_counts["quarantined"] == 1


@pytest.mark.parametrize(
    "value",
    [
        "https://jobs.smartrecruiters.com.evil/acme/1",
        "http://jobs.smartrecruiters.com/acme/1",
        "https://jobs.smartrecruiters.com/Other/1",
    ],
)
def test_canonical_url_gate_rejects_wrong_origin_or_board(value):
    assert not canonical_posting_url(value, "acme")


def test_apply_url_gate():
    assert valid_apply_url("https://jobs.smartrecruiters.com/acme/1/apply")
    assert not valid_apply_url("https://jobs.smartrecruiters.com/")


def test_title_prefilter_keeps_missing_index_metadata_and_skips_unrelated_titles():
    def handler(request):
        if request.url.path.endswith("/postings"):
            unrelated = posting("2")
            unrelated["name"] = "Accountant"
            return index(request, [posting("1"), unrelated])
        return httpx.Response(200, json=detail("1"), request=request)

    collector, result = collect(handler, title_filter=lambda title: "Software Engineer" in title)
    assert result.status == CollectionStatus.SUCCESS
    assert collector.last_counts["detail_requests"] == 1
    assert collector.last_counts["prefilter_suppressed"] == 1


@pytest.mark.parametrize(
    "timestamp,expected",
    [
        ("2026-10-01T13:00:00+01:00", datetime(2026, 10, 1, 12, tzinfo=UTC)),
        (None, None),
        ("2026-10-01T12:00:00", None),
    ],
)
def test_timestamp_preserves_age_and_never_invents_timezone(timestamp, expected):
    def handler(request):
        if request.url.path.endswith("/postings"):
            body = posting("1")
            body["releasedDate"] = timestamp
            return index(request, [body])
        body = detail("1")
        body["releasedDate"] = timestamp
        return httpx.Response(200, json=body, request=request)

    _, result = collect(handler)
    assert result.jobs[0].posted_at == expected


def test_later_page_failure_retains_verified_jobs_as_partial():
    def handler(request):
        if request.url.path.endswith("/postings"):
            if request.url.params["offset"] == "0":
                return httpx.Response(
                    200,
                    json={"limit": 100, "offset": 0, "totalFound": 2, "content": [posting("1")]},
                    request=request,
                )
            return httpx.Response(503, request=request)
        return httpx.Response(200, json=detail("1"), request=request)

    _, result = collect(handler)
    assert result.status == CollectionStatus.PARTIAL
    assert len(result.jobs) == 1
    assert result.errors


@pytest.mark.parametrize(
    "status,classification",
    [
        (200, "valid_empty"),
        (404, "invalid"),
        (403, "restricted"),
        (429, "rate_limited"),
        (503, "transient_failure"),
    ],
)
def test_registry_health_classifications(status, classification):
    from validation.target_universe_health_v1.run import HealthProbe

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status,
                json={"limit": 1, "offset": 0, "totalFound": 0, "content": []},
                request=request,
            )
        )
    ) as client:
        result = HealthProbe(client, delay=0).probe(
            {
                "target_identity": "smartrecruiters:acme",
                "source": "smartrecruiters",
                "coordinates": {"board": "acme"},
                "historical_occurrence_count": 1,
            }
        )
    assert result["classification"] == classification


def test_provider_registry_sharding_uses_existing_boundary():
    from job_scout.production_registry import (
        ProductionSourceRegistry,
        ProductionTarget,
        build_shard_manifest,
    )
    from job_scout.shard_collection import default_collector_factory

    record = ProductionTarget(
        target_identity="smartrecruiters:acme",
        source="smartrecruiters",
        coordinates={"board": "acme"},
        company_hint="Acme",
    )
    registry = ProductionSourceRegistry(
        registry_id="test",
        target_universe_git_blob_sha="a" * 40,
        health_manifest_sha256="b" * 64,
        health_evidence_updated_at="2026-10-01T12:00:00Z",
        target_counts_by_source={"smartrecruiters": 1},
        targets=[record],
    )
    manifest = build_shard_manifest(registry, shard_counts_by_source={"smartrecruiters": 1})
    assert manifest.shards[0].target_identities == ["smartrecruiters:acme"]
    collector = default_collector_factory("smartrecruiters")
    collector.client.close()


def test_benchmark_uses_existing_batch_policy_and_history(tmp_path):
    from job_scout.search_brief import load_search_brief
    from validation.smartrecruiters_provider_v1.run import measure_deliverable_supply

    def handler(request):
        if request.url.path.endswith("/postings"):
            return index(request, [posting("1"), posting("2")])
        return httpx.Response(
            200, json=detail(request.url.path.rsplit("/", 1)[-1]), request=request
        )

    _, result = collect(handler)
    jobs = [job.model_copy(update={"posted_at": datetime.now(UTC)}) for job in result.jobs]
    brief = load_search_brief("config/search_briefs/taiwo_software_remote_us_v1.json")
    supply = measure_deliverable_supply(jobs, brief)
    assert supply["dry_run_deliverable_jobs"] == 1
    assert supply["company_cap_suppressed_groups"] == 1
    assert supply["history_replay_suppressed_groups"] == 1


def test_benchmark_continues_after_unexpected_target_exception(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    from validation.smartrecruiters_provider_v1 import run

    closed = []

    class Collector:
        def __init__(self, **kwargs):
            self.last_counts = {}
            self.client = SimpleNamespace(close=lambda: closed.append(True))

        def collect(self, item):
            if item.board_id == "bad":
                raise RuntimeError("unexpected collector failure")
            from job_scout.domain.models import CollectionResult

            return CollectionResult(
                source="smartrecruiters",
                target=item,
                status=CollectionStatus.SUCCESS,
                jobs=[],
                raw_postings_received=0,
            )

    monkeypatch.setattr(run, "SmartRecruitersCollector", Collector)
    report = run.run_benchmark(
        config=run.BenchmarkConfig(
            targets=[
                run.TargetConfig(identifier="bad", company="Bad"),
                run.TargetConfig(identifier="good", company="Good"),
            ]
        ),
        brief_path=Path("config/search_briefs/taiwo_software_remote_us_v1.json"),
    )
    assert report["totals"]["targets_attempted"] == 2
    assert report["totals"]["targets_failed"] == 1
    assert report["totals"]["targets_successful"] == 1
    assert len(closed) == 2


def test_missing_index_content_is_malformed_not_valid_empty():
    _, result = collect(
        lambda request: httpx.Response(
            200, json={"limit": 100, "offset": 0, "totalFound": 0}, request=request
        )
    )
    assert result.status == CollectionStatus.PARSE_FAILURE


def test_invalid_posting_path_cannot_be_hydrated():
    malformed = posting("1")
    malformed["id"] = "../other?destination=INTERNAL"
    collector, result = collect(lambda request: index(request, [malformed]))
    assert result.status == CollectionStatus.PARTIAL
    assert collector.last_counts["detail_requests"] == 0
    assert collector.last_counts["quarantined"] == 1


def test_history_recognizes_posting_id_across_slug_and_apply_url_changes(tmp_path):
    from job_scout.history import source_identity
    from job_scout.storage.sqlite import SQLiteRepository

    _, result = collect(
        lambda request: (
            index(request, [posting("1")])
            if request.url.path.endswith("/postings")
            else httpx.Response(200, json=detail("1"), request=request)
        )
    )
    job = result.jobs[0]
    repo = SQLiteRepository(tmp_path / "jobs.db")
    repo.upsert_job(job)
    prior = "https://jobs.smartrecruiters.com/Acme/1-previous-title/apply?source=old"
    assert source_identity(prior) == ("smartrecruiters", "acme", "1")
    repo.observe_destination_links(client_id="client", destination="sheet-a", links=[prior])
    assert repo.is_historically_surfaced(job, client_id="client", destination="sheet-a")
    assert not repo.is_historically_surfaced(job, client_id="client", destination="sheet-b")


def test_partial_detail_location_merges_missing_index_evidence():
    def handler(request):
        if request.url.path.endswith("/postings"):
            body = posting("1")
            body["location"] = {
                "city": "Lagos",
                "region": "Lagos",
                "country": "ng",
                "remote": True,
                "locationType": "remote",
            }
            return index(request, [body])
        body = detail("1")
        body["location"] = {"city": "Lagos"}
        return httpx.Response(200, json=body, request=request)

    _, result = collect(handler)

    job = result.jobs[0]
    assert job.country == "Nigeria"
    assert job.region == "Lagos"
    assert job.city == "Lagos"
    assert job.remote_status.value == "remote"


def test_source_identity_survives_optional_uuid_and_board_case_changes():
    def handler(request):
        if request.url.path.endswith("/postings"):
            return index(request, [posting("1")])
        body = detail("1")
        body.pop("uuid")
        return httpx.Response(200, json=body, request=request)

    _, without_uuid = collect(handler)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        differently_cased = SmartRecruitersCollector(client).collect(
            SourceTarget(board_id="Acme", company="Acme Inc", employer_id="acme")
        )
    assert without_uuid.jobs[0].id == differently_cased.jobs[0].id


def test_raw_received_is_not_advertised_board_total():
    def handler(request):
        if request.url.path.endswith("/postings"):
            return httpx.Response(
                200,
                json={
                    "limit": 100,
                    "offset": 0,
                    "totalFound": 10000,
                    "content": [posting("1"), posting("2")],
                },
                request=request,
            )
        return httpx.Response(
            200, json=detail(request.url.path.rsplit("/", 1)[-1]), request=request
        )

    collector, result = collect(handler, max_postings=2)
    assert result.status == CollectionStatus.PARTIAL
    assert result.raw_postings_received == 2
    assert collector.last_counts["provider_reported_total"] == 10000


@pytest.mark.parametrize(
    "change",
    [
        {"targets_failed": 1},
        {"targets_partial": 1},
        {"canonical_verified_postings": 0},
        {"valid_apply_url_postings": 0},
    ],
)
def test_capacity_gate_fails_closed(change):
    from validation.smartrecruiters_provider_v1.run import validate_benchmark_report

    totals = {
        "targets_failed": 0,
        "targets_partial": 0,
        "normalized_jobs": 1,
        "canonical_verified_postings": 1,
        "valid_apply_url_postings": 1,
        "postings_with_trustworthy_age": 1,
        **change,
    }
    with pytest.raises(ValueError):
        validate_benchmark_report({"totals": totals})


@pytest.mark.parametrize("total", [0, 1])
def test_index_rows_cannot_exceed_advertised_total(total):
    def handler(request):
        if not request.url.path.endswith("/postings"):
            pytest.fail("malformed page must not be hydrated")
        return httpx.Response(
            200,
            json={
                "limit": 100,
                "offset": 0,
                "totalFound": total,
                "content": [posting("1"), posting("2")],
            },
            request=request,
        )

    collector, result = collect(handler)
    assert result.status == CollectionStatus.PARSE_FAILURE
    assert result.raw_postings_received == 2
    assert collector.last_counts["detail_requests"] == 0


def test_production_smartrecruiters_requires_canonical_identity():
    from pydantic import ValidationError

    from job_scout.production_registry import ProductionTarget

    with pytest.raises(ValidationError, match="identity does not match coordinates"):
        ProductionTarget(
            target_identity="smartrecruiters:Acme",
            source="smartrecruiters",
            coordinates={"board": "Acme"},
            company_hint="Acme",
        )
    record = ProductionTarget(
        target_identity="smartrecruiters:acme",
        source="smartrecruiters",
        coordinates={"board": "Acme"},
        company_hint="Acme",
    )
    assert record.source_target().board_id == "acme"


@pytest.mark.parametrize("url", [
    "https://example.com/Acme/1-engineer",
    "http://jobs.smartrecruiters.com/Acme/1-engineer",
    "https://jobs.smartrecruiters.com/Other/1-engineer",
    "https://jobs.smartrecruiters.com/Acme/2-engineer",
    "https://jobs.smartrecruiters.com/Acme/10-engineer",
])
def test_hydrated_canonical_url_must_match_posting_identity(url):
    def handler(request):
        if request.url.path.endswith("/postings"):
            return index(request, [posting("1")])
        body = detail("1")
        body["postingUrl"] = url
        return httpx.Response(200, json=body, request=request)

    collector, result = collect(handler)
    assert result.jobs == []
    assert collector.last_counts["quarantined"] == 1


def test_single_board_cli_exposes_smartrecruiters(monkeypatch, tmp_path):
    import json
    import sys

    import job_scout.cli as cli

    client = tmp_path / "client.json"
    client.write_text("{}")
    captured = {}

    def fake_run_pipeline(**kwargs):
        captured.update(kwargs)

        class Summary:
            collected = 0
            matched = 0
            delivered = 0

        return Summary()

    monkeypatch.setattr(cli, "load_search_brief", lambda _: object())
    monkeypatch.setattr(cli, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "job-scout",
            "collect",
            "--source",
            "smartrecruiters",
            "--client",
            str(client),
            "--board",
            "Acme",
            "--company",
            "Acme",
            "--database",
            str(tmp_path / "jobs.sqlite3"),
            "--csv",
            str(tmp_path / "jobs.csv"),
        ],
    )

    cli.main()

    assert isinstance(captured["collector"], SmartRecruitersCollector)
    assert captured["target"].board_id == "Acme"
