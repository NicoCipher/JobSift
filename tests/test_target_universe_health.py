from __future__ import annotations

import os
from collections import Counter

import httpx
import pytest

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


def test_workday_cap_is_provider_reported_not_exact() -> None:
    item = target(
        "workday", "workday:h:t:s", {"host": "h.wd1.myworkdayjobs.com", "tenant": "h", "site": "s"}
    )
    value = probe(httpx.Response(200, json={"total": 2000}), item)
    assert value["provider_reported_total"] == 2000
    assert value["inventory_exact"] is False
    assert "capped" in value["inventory_note"]


def test_restricted_rate_limited_and_malformed_are_distinct() -> None:
    item = target("ashby", "ashby:acme", {"board": "acme"})
    assert probe(httpx.Response(403), item)["classification"] == "restricted"
    assert probe(httpx.Response(429), item)["classification"] == "rate_limited"
    assert (
        probe(httpx.Response(200, content=b"not json"), item)["classification"]
        == "malformed_response"
    )
    rejected = probe(httpx.Response(422, json={"errorCode": "HTTP_422"}), item)
    assert rejected["classification"] == "unprocessable"
    assert "provider rejected" in rejected["error"]


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
            "source": "greenhouse",
            "classification": "active",
        },
    )
    assert run.completed(target_value, manifest)["classification"] == "active"
    run.write_json(path, {"manifest_sha256": "other", "target_identity": "greenhouse:acme"})
    assert run.completed(target_value, manifest) is None


def test_full_manifest_has_exact_canonical_universe() -> None:
    universe = {
        "target_records": [
            *[
                target("greenhouse", f"greenhouse:{index}", {"board": str(index)})
                for index in range(774)
            ],
            *[target("ashby", f"ashby:{index}", {"board": str(index)}) for index in range(678)],
            *[
                target(
                    "workday",
                    f"workday:{index}",
                    {"host": "h", "tenant": "t", "site": str(index)},
                )
                for index in range(560)
            ],
            *[
                target("lever", f"lever:global:{index}", {"instance": "global", "site": str(index)})
                for index in range(275)
            ],
        ]
    }
    manifest = run.make_full_manifest(universe, generated_at="2026-09-08T00:00:00+00:00")
    assert len(manifest["targets"]) == 2287
    assert manifest["source_counts"] == {
        "greenhouse": 774,
        "ashby": 678,
        "workday": 560,
        "lever": 275,
    }


def test_lever_exact_pagination_and_repeated_page_protection() -> None:
    item = target("lever", "lever:global:x", {"instance": "global", "site": "x"})
    pages = [
        [{"id": str(index)} for index in range(50)],
        [{"id": str(index)} for index in range(50, 53)],
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[int(request.url.params["skip"]) // 50])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        value = run.HealthProbe(client, delay=0, exact_lever_count=True).probe(item)
    assert value["classification"] == "active"
    assert value["current_postings"] == 53
    assert value["request_count"] == 2
    assert value["inventory_exact"] is True

    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=pages[0]))
    ) as client:
        repeated = run.HealthProbe(client, delay=0, exact_lever_count=True).probe(item)
    assert repeated["classification"] == "malformed_response"
    assert repeated["error"] == "repeated Lever page signature"


def test_full_checkpoint_mismatch_is_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(run, "FULL_CHECKPOINTS", tmp_path / "full")
    item = target("greenhouse", "greenhouse:acme", {"board": "acme"})
    manifest = {"manifest_sha256": "expected"}
    run.write_json(
        run.checkpoint_path(item, full=True),
        {"manifest_sha256": "wrong", "target_identity": item["target_identity"]},
    )
    with pytest.raises(ValueError, match="mismatched full checkpoint"):
        run.completed(item, manifest, full=True)


def test_full_summary_reconciles_inventory_and_frequency() -> None:
    manifest = {
        "manifest_sha256": "m",
        "targets": [
            target("greenhouse", "greenhouse:a", {"board": "a"}, 1),
            target("ashby", "ashby:b", {"board": "b"}, 4),
            target("workday", "workday:c", {"host": "h", "tenant": "t", "site": "s"}, 10),
            target("lever", "lever:global:d", {"instance": "global", "site": "d"}, 25),
        ],
    }
    values = [
        {
            "source": "greenhouse",
            "target_identity": "greenhouse:a",
            "classification": "active",
            "historical_occurrence_count": 1,
            "current_postings": 8,
            "inventory_exact": True,
        },
        {
            "source": "ashby",
            "target_identity": "ashby:b",
            "classification": "valid_empty",
            "historical_occurrence_count": 4,
            "current_postings": 0,
            "inventory_exact": True,
        },
        {
            "source": "workday",
            "target_identity": "workday:c",
            "classification": "active",
            "historical_occurrence_count": 10,
            "current_postings": 2000,
            "inventory_exact": False,
        },
        {
            "source": "lever",
            "target_identity": "lever:global:d",
            "classification": "unprocessable",
            "historical_occurrence_count": 25,
            "current_postings": None,
            "inventory_exact": None,
        },
    ]
    summary = run.summarize_full(values, manifest)
    assert Counter(summary["classification_reconciliation"]).total() == 4
    assert summary["overall"]["raw_current_posting_evidence"] == 2008
    assert summary["per_source"]["workday"]["potentially_capped_workday_targets"] == 1
    assert summary["historical_frequency_vs_health"][0]["active_percentage"] == 100.0


def test_full_batch_resume_does_not_repeat_completed_targets(tmp_path, monkeypatch) -> None:
    items = [
        target("greenhouse", f"greenhouse:{letter}", {"board": letter})
        for letter in ("a", "b", "c")
    ]
    manifest = {"manifest_sha256": "full-manifest", "targets": items}
    monkeypatch.setattr(run, "FULL_CHECKPOINTS", tmp_path / "checkpoints")
    monkeypatch.setattr(run, "FULL_RESULTS", tmp_path / "results.json")
    monkeypatch.setattr(run, "FULL_SUMMARY", tmp_path / "summary.json")
    monkeypatch.setattr(run, "FULL_REPORT", tmp_path / "report.md")
    monkeypatch.setattr(run, "load_full_manifest", lambda: manifest)
    calls: list[str] = []

    class FakeProbe:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def probe(self, item):
            calls.append(item["target_identity"])
            return {
                "target_identity": item["target_identity"],
                "source": item["source"],
                "historical_occurrence_count": item["historical_occurrence_count"],
                "classification": "active",
                "current_postings": 1,
                "inventory_exact": True,
            }

    monkeypatch.setattr(run, "HealthProbe", FakeProbe)
    assert run.run_full(delay=0, batch_size=2)["remaining"] == 1
    assert run.run_full(delay=0, batch_size=2)["remaining"] == 0
    assert calls == [item["target_identity"] for item in items]
    assert (tmp_path / "summary.json").exists()
    assert "job_scout" not in run.__dict__


def test_full_run_lock_rejects_concurrent_resume(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(run, "FULL_ROOT", tmp_path)
    monkeypatch.setattr(run, "FULL_LOCK", tmp_path / "run.lock")
    run.write_json(run.FULL_LOCK, {"pid": os.getpid()})
    with pytest.raises(RuntimeError, match="already running"), run.full_run_lock():
        pass
