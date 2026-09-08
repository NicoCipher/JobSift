from __future__ import annotations

import httpx

from validation.target_universe_health_v1 import run


def target(source: str, identity: str, coordinates: dict[str, str], count: int = 1):
    return {
        "source": source,
        "target_identity": identity,
        "coordinates": coordinates,
        "historical_occurrence_count": count,
    }


def probe(response: httpx.Response | Exception, item: dict[str, object]):
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(response, Exception):
            raise response
        return response

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return run.HealthProbe(client, delay=0).probe(item)


def test_greenhouse_active_empty_and_invalid() -> None:
    item = target("greenhouse", "greenhouse:acme", {"board": "acme"})
    assert probe(httpx.Response(200, json={"jobs": [{}]}), item)["classification"] == "active"
    assert probe(httpx.Response(200, json={"jobs": []}), item)["classification"] == "valid_empty"
    assert probe(httpx.Response(404), item)["classification"] == "invalid"


def test_ashby_active_and_malformed_payload() -> None:
    item = target("ashby", "ashby:acme", {"board": "acme"})
    assert probe(httpx.Response(200, json={"jobs": [{}]}), item)["classification"] == "active"
    assert (
        probe(httpx.Response(200, json={"no_jobs": []}), item)["classification"]
        == "malformed_response"
    )


def test_lever_global_eu_and_empty_routing() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=[])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        health = run.HealthProbe(client, delay=0)
        assert (
            health.probe(target("lever", "lever:global:x", {"instance": "global", "site": "x"}))[
                "classification"
            ]
            == "valid_empty"
        )
        assert (
            health.probe(target("lever", "lever:eu:y", {"instance": "eu", "site": "y"}))[
                "classification"
            ]
            == "valid_empty"
        )
    assert "api.lever.co" in seen[0] and "api.eu.lever.co" in seen[1]


def test_workday_uses_one_search_and_no_detail() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"total": 7, "jobPostings": []})

    item = target(
        "workday", "workday:h:t:s", {"host": "h.wd1.myworkdayjobs.com", "tenant": "h", "site": "s"}
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        value = run.HealthProbe(client, delay=0).probe(item)
    assert value["classification"] == "active" and value["provider_reported_total"] == 7
    assert seen == ["https://h.wd1.myworkdayjobs.com/wday/cxs/h/s/jobs"]


def test_workday_empty_and_transient_statuses() -> None:
    item = target(
        "workday", "workday:h:t:s", {"host": "h.wd1.myworkdayjobs.com", "tenant": "h", "site": "s"}
    )
    assert probe(httpx.Response(200, json={"total": 0}), item)["classification"] == "valid_empty"
    assert probe(httpx.Response(503), item)["classification"] == "transient_failure"
    assert probe(httpx.ReadTimeout("timeout"), item)["classification"] == "transient_failure"


def test_restricted_rate_limited_and_malformed_are_distinct() -> None:
    item = target("ashby", "ashby:acme", {"board": "acme"})
    assert probe(httpx.Response(403), item)["classification"] == "restricted"
    assert probe(httpx.Response(429), item)["classification"] == "rate_limited"
    assert (
        probe(httpx.Response(200, content=b"not json"), item)["classification"]
        == "malformed_response"
    )
    malformed = probe(httpx.Response(422), item)
    assert malformed["classification"] == "malformed_response" and malformed["error"] == "HTTP 422"


def test_selection_is_deterministic_exact_and_keeps_positive_controls() -> None:
    records = []
    coordinate = {
        "greenhouse": {"board": "x"},
        "ashby": {"board": "x"},
        "workday": {"host": "x.wd1.myworkdayjobs.com", "tenant": "x", "site": "s"},
        "lever": {"instance": "global", "site": "x"},
    }
    for source in run.SOURCES:
        control = run.CONTROLS[source]
        records.append(target(source, control, coordinate[source], 0))
        records.extend(
            target(source, f"{source}:sample-{index}", coordinate[source], 100 - index)
            for index in range(30)
        )
    first = run.select_pilot(records)
    second = run.select_pilot(list(reversed(records)))
    assert [value["target_identity"] for value in first] == [
        value["target_identity"] for value in second
    ]
    assert {
        source: sum(value["source"] == source for value in first) for source in run.SOURCES
    } == {source: 25 for source in run.SOURCES}
    assert set(run.CONTROLS.values()) <= {value["target_identity"] for value in first}


def test_checkpoint_resume_and_mismatch_rejection(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(run, "CHECKPOINTS", tmp_path / "checkpoints")
    target_value = target("greenhouse", "greenhouse:acme", {"board": "acme"})
    manifest = {"manifest_sha256": "manifest"}
    path = run.checkpoint_path(target_value)
    run.write_json(
        path,
        {
            "manifest_sha256": "manifest",
            "target_identity": "greenhouse:acme",
            "classification": "active",
        },
    )
    assert run.completed(target_value, manifest)["classification"] == "active"
    run.write_json(path, {"manifest_sha256": "other", "target_identity": "greenhouse:acme"})
    assert run.completed(target_value, manifest) is None
