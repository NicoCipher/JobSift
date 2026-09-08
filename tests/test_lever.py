from __future__ import annotations

import sys
import uuid
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from job_scout import cli
from job_scout.collectors.lever import LeverCollector
from job_scout.domain.models import (
    CollectionStatus,
    LeverTargetConfig,
    RemoteStatus,
    SourceTarget,
)


def config(instance: str = "global", site: str = "acme") -> LeverTargetConfig:
    return LeverTargetConfig(instance=instance, site=site)  # type: ignore[arg-type]


def target(instance: str = "global", site: str = "acme") -> SourceTarget:
    lever = config(instance, site)
    return SourceTarget(board_id=lever.board_id, company="Acme", lever=lever)


def raw_job(identifier: str = "provider-1", **changes: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "id": identifier,
        "text": "Technical Support Engineer",
        "hostedUrl": "https://jobs.lever.co/acme/provider-1?utm_source=board",
        "applyUrl": "https://jobs.lever.co/acme/provider-1/apply?ref=board",
        "description": "<p>Fallback <strong>description</strong></p>",
        "descriptionPlain": "Plain description",
        "descriptionBodyPlain": "Body description",
        "country": "US",
        "workplaceType": "remote",
        "createdAt": 1710000000000,
        "salaryRange": {"min": 100000},
        "categories": {
            "location": "Austin, TX, United States",
            "allLocations": ["Austin, TX, United States", "Remote, United States"],
            "commitment": "Full-time",
            "team": "Customer Engineering",
            "department": "Support",
        },
    }
    raw.update(changes)
    return raw


def collector_for(
    responses: list[object], requests: list[httpx.Request] | None = None
) -> LeverCollector:
    response_iter = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        response = next(response_iter)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, httpx.Response)
        return response

    return LeverCollector(httpx.Client(transport=httpx.MockTransport(handler)), delay=0)


def response(payload: object, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def test_global_api_host_identity_and_urls() -> None:
    requests: list[httpx.Request] = []
    result = collector_for([response([raw_job()])], requests).collect(target())

    assert result.status is CollectionStatus.SUCCESS
    assert requests[0].url.host == "api.lever.co"
    assert requests[0].url.path == "/v0/postings/acme"
    assert dict(requests[0].url.params) == {"mode": "json", "skip": "0", "limit": "50"}
    job = result.jobs[0]
    assert job.source_board_id == "global:acme"
    assert job.source_job_id == "provider-1"
    assert job.id == str(uuid.uuid5(uuid.NAMESPACE_URL, "lever:global:acme:provider-1"))
    assert str(job.job_url) == "https://jobs.lever.co/acme/provider-1"
    assert str(job.canonical_url) == "https://jobs.lever.co/acme/provider-1"
    assert str(job.apply_url) == "https://jobs.lever.co/acme/provider-1/apply"


def test_eu_host_and_instance_site_identity_are_distinct() -> None:
    eu = collector_for([response([raw_job()])]).collect(target("eu", "acme"))
    global_site = collector_for([response([raw_job()])]).collect(target("global", "other"))
    eu_request: list[httpx.Request] = []
    collector_for([response([])], eu_request).collect(target("eu", "acme"))

    assert eu_request[0].url.host == "api.eu.lever.co"
    assert eu.jobs[0].id != global_site.jobs[0].id
    assert eu.jobs[0].id != collector_for([response([raw_job()])]).collect(target()).jobs[0].id


@pytest.mark.parametrize("instance", ["", "us", "GLOBAL"])
def test_target_instance_validation(instance: str) -> None:
    with pytest.raises(ValidationError):
        LeverTargetConfig(instance=instance, site="acme")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        LeverTargetConfig(instance="global", site="white space")


def test_short_and_multi_page_pagination_advance_by_fifty() -> None:
    requests: list[httpx.Request] = []
    first = [raw_job(f"first-{number}") for number in range(50)]
    second = [raw_job(f"second-{number}") for number in range(50)]
    final = [raw_job("final")]
    result = collector_for([response(first), response(second), response(final)], requests).collect(
        target()
    )

    assert result.status is CollectionStatus.SUCCESS
    assert len(result.jobs) == 101
    assert [request.url.params["skip"] for request in requests] == ["0", "50", "100"]


def test_empty_board_is_success() -> None:
    result = collector_for([response([])]).collect(target())

    assert result.status is CollectionStatus.SUCCESS
    assert result.jobs == [] and result.errors == []


def test_repeated_page_protection_is_partial() -> None:
    page = [raw_job(str(number)) for number in range(50)]
    result = collector_for([response(page), response(page)]).collect(target())

    assert result.status is CollectionStatus.PARTIAL
    assert len(result.jobs) == 50
    assert "repeated page signature" in result.errors[0]


def test_404_is_invalid_target() -> None:
    result = collector_for([response([], 404)]).collect(target())

    assert result.status is CollectionStatus.INVALID_TARGET


def test_401_and_403_keep_existing_failure_classifications() -> None:
    unauthorized = collector_for([response([], 401)]).collect(target())
    forbidden = collector_for([httpx.Response(403, text="Forbidden")]).collect(target())
    throttled = collector_for([httpx.Response(403, text="Rate limit exceeded")]).collect(target())

    assert unauthorized.status is CollectionStatus.AUTHENTICATION_FAILURE
    assert forbidden.status is CollectionStatus.FORBIDDEN
    assert throttled.status is CollectionStatus.RATE_LIMITED


def test_429_and_5xx_are_retried_then_classified() -> None:
    limited = collector_for([response([], 429), response([], 429), response([], 429)]).collect(
        target()
    )
    failed = collector_for([response([], 500), response([], 503), response([], 500)]).collect(
        target()
    )

    assert limited.status is CollectionStatus.RATE_LIMITED
    assert failed.status is CollectionStatus.PROVIDER_ERROR


def test_timeout_is_retried_then_network_failure() -> None:
    request = httpx.Request("GET", "https://api.lever.co/v0/postings/acme")
    result = collector_for(
        [
            httpx.ReadTimeout("one", request=request),
            httpx.ReadTimeout("two", request=request),
            httpx.ReadTimeout("three", request=request),
        ]
    ).collect(target())

    assert result.status is CollectionStatus.NETWORK_FAILURE
    assert result.errors == ["ReadTimeout"]


def test_malformed_top_level_json_is_parse_failure() -> None:
    malformed = httpx.Response(200, text="not json")
    result = collector_for([malformed]).collect(target())

    assert result.status is CollectionStatus.PARSE_FAILURE


def test_malformed_and_blank_identity_postings_are_quarantined() -> None:
    malformed = raw_job("ok", hostedUrl="not a URL")
    result = collector_for([response([raw_job("good"), malformed, raw_job(" ")])]).collect(target())

    assert result.status is CollectionStatus.PARTIAL
    assert [job.source_job_id for job in result.jobs] == ["good"]
    assert len(result.errors) == 2
    assert "provider id is missing" in result.errors[1]


@pytest.mark.parametrize(
    ("workplace", "expected"),
    [
        ("remote", RemoteStatus.REMOTE),
        ("hybrid", RemoteStatus.HYBRID),
        ("onsite", RemoteStatus.ONSITE),
        ("future-value", RemoteStatus.UNKNOWN),
    ],
)
def test_workplace_mapping_is_explicit(workplace: str, expected: RemoteStatus) -> None:
    result = collector_for([response([raw_job(workplaceType=workplace)])]).collect(target())

    assert result.jobs[0].remote_status is expected


def test_description_fallback_and_missing_description_are_valid() -> None:
    fallback = raw_job(
        descriptionPlain=" ", descriptionBodyPlain=" ", description="<p>HTML fallback</p>"
    )
    missing = raw_job("missing", descriptionPlain=None, descriptionBodyPlain=None, description=None)
    result = collector_for([response([fallback, missing])]).collect(target())

    assert result.status is CollectionStatus.SUCCESS
    assert result.jobs[0].description_text == "HTML fallback"
    assert result.jobs[1].description_text is None


def test_locations_country_department_and_timestamps_are_conservative() -> None:
    result = collector_for([response([raw_job(country="ZZ")])]).collect(target())
    job = result.jobs[0]

    assert job.offices == ["Austin, TX, United States", "Remote, United States"]
    assert not job.eligible_countries and job.country is None
    assert job.department == "Support"
    assert job.raw_metadata["team"] == "Customer Engineering"
    assert job.posted_at is None and job.updated_at is None
    assert job.raw_metadata["createdAt"] == 1710000000000
    assert "description" not in job.raw_metadata


def test_no_detail_endpoint_calls_are_made() -> None:
    requests: list[httpx.Request] = []
    collector_for([response([raw_job()])], requests).collect(target())

    assert [request.url.path for request in requests] == ["/v0/postings/acme"]


def test_collector_requires_explicit_matching_target_configuration() -> None:
    no_config = LeverCollector(delay=0).collect(
        SourceTarget(board_id="global:acme", company="Acme")
    )
    wrong_board = LeverCollector(delay=0).collect(
        SourceTarget(board_id="wrong", company="Acme", lever=config())
    )

    assert no_config.status is CollectionStatus.INVALID_TARGET
    assert wrong_board.status is CollectionStatus.INVALID_TARGET


def test_cli_routes_explicit_lever_target(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_pipeline(**kwargs: object) -> SimpleNamespace:
        seen.update(kwargs)
        return SimpleNamespace(status="success")

    monkeypatch.setattr(cli, "run_pipeline", fake_pipeline)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "job-scout",
            "collect",
            "--source",
            "lever",
            "--lever-instance",
            "eu",
            "--board",
            "acme",
            "--company",
            "Acme",
            "--client",
            "config/search_briefs/taiwo_operator_sourcing_v1.json",
        ],
    )

    cli.main()

    selected = seen["target"]
    assert isinstance(selected, SourceTarget)
    assert selected.board_id == "eu:acme"
    assert selected.lever == LeverTargetConfig(instance="eu", site="acme")
    assert isinstance(seen["collector"], LeverCollector)
