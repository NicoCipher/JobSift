from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from job_scout.domain.models import CollectionResult, CollectionStatus, Job, RemoteStatus
from job_scout.storage.sqlite import SQLiteRepository
from validation.taiwo_sourcing_capacity_v1.stage_b import run as stage_b


def _job(number: str, *, source: str = "workday", url: str | None = None) -> Job:
    url = url or f"https://example.test/jobs/{source}-{number}"
    return Job(
        id=f"{source}-{number}",
        source=source,
        source_board_id="tenant:site",
        source_job_id=number,
        title="Support Engineer",
        company="Acme",
        description_text="Troubleshoot Linux networking and Active Directory customer incidents. "
        * 12,
        job_url=url,
        canonical_url=url,
        country="United States",
        remote_status=RemoteStatus.REMOTE,
        department="Support",
        content_fingerprint=f"fingerprint-{source}-{number}",
    )


def _paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = tmp_path / "stage-b/runtime"
    monkeypatch.setattr(stage_b, "RUNTIME", runtime)
    monkeypatch.setattr(stage_b, "LOCK", runtime / "run.lock")
    monkeypatch.setattr(stage_b, "DATABASE", runtime / "replay.sqlite3")
    monkeypatch.setattr(stage_b, "CONTINUITY", runtime / "continuity.json")
    monkeypatch.setattr(stage_b, "INCREMENTAL_DELIVERIES", runtime / "incremental_deliveries.csv")
    monkeypatch.setattr(stage_b, "SUMMARY_PATH", tmp_path / "stage-b/summary.json")
    monkeypatch.setattr(stage_b, "REPORT_PATH", tmp_path / "stage-b/report.md")


def _target(
    number: int, *, tier: str = "exact_1_49", estimate: int | None = 10
) -> dict[str, object]:
    return {
        "source": "workday",
        "target_identity": f"workday:test-{number}:tenant:site-{number}",
        "host": f"test-{number}.example.test",
        "tenant": "tenant",
        "site": f"site-{number}",
        "company_hint": "Acme",
        "historical_occurrence_count": 1,
        "health_current_postings": 5,
        "inventory_exact": tier != "potentially_capped",
        "potentially_capped": tier == "potentially_capped",
        "cost_tier": tier,
        "estimated_list_requests": 1 if estimate else None,
        "estimated_detail_requests": estimate - 1 if estimate else None,
        "estimated_total_requests": estimate,
        "uncertainty_reason": "unknown" if estimate is None else None,
    }


def _manifest(targets: list[dict[str, object]]) -> dict[str, object]:
    return {"manifest_sha256": "frozen", "targets": targets}


def _stage_a_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, object]:
    _paths(monkeypatch, tmp_path)
    monkeypatch.setattr(stage_b, "EXPECTED_STAGE_A_JOBS", 44)
    monkeypatch.setattr(stage_b, "EXPECTED_STAGE_A_HISTORICAL_LINKS", 0)
    root = tmp_path / "stage-a"
    database = root / "runtime/replay.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    repository = SQLiteRepository(database)
    for number in range(44):
        job = _job(str(number), source="greenhouse", url=f"https://history.test/{number}")
        job.title = f"Support Engineer {number}"
        job.description_text = f"{job.description_text} Historical evidence {number}."
        repository.upsert_job(job)
        repository.mark_exported(job.id, "taiwo_operator_sourcing_v1", "stage-a-deliveries")
    core = {"stage": "a", "generated_at": "2026-01-01T00:00:00+00:00"}
    core["manifest_sha256"] = stage_b.sha_value(core)
    manifest_path, summary_path = root / "manifest.json", root / "summary.json"
    stage_b.write_json(manifest_path, core)
    stage_b.write_json(
        summary_path,
        {
            "manifest_sha256": core["manifest_sha256"],
            "fresh_unique_deliveries": 44,
            "stop_reason": "all_stage_a_targets_completed",
        },
    )
    monkeypatch.setattr(stage_b, "STAGE_A_DATABASE", database)
    monkeypatch.setattr(stage_b, "STAGE_A_MANIFEST", manifest_path)
    monkeypatch.setattr(stage_b, "STAGE_A_SUMMARY", summary_path)
    manifest = _manifest([_target(1)])
    manifest.update(stage_b._stage_a_evidence())
    return manifest


class _Counter:
    def __init__(self) -> None:
        self.client_request_calls = 1

    def close(self) -> None:
        return None


class _Collector:
    def __init__(
        self, jobs: list[Job], status: CollectionStatus = CollectionStatus.SUCCESS
    ) -> None:
        self.jobs, self.status = jobs, status
        self.last_counts = {
            "broad_total": len(jobs),
            "paths_discovered": len(jobs),
            "detail_attempts": len(jobs),
            "normalized": len(jobs),
            "quarantined": int(status is CollectionStatus.PARTIAL),
            "partition_count": 0,
            "coverage_mode": "broad",
        }

    def collect(self, target):
        return CollectionResult(
            source="workday",
            target=target,
            status=self.status,
            jobs=self.jobs,
            errors=[] if self.status is CollectionStatus.SUCCESS else [self.status.value],
        )


def _responses(
    monkeypatch: pytest.MonkeyPatch, responses: list[tuple[list[Job], CollectionStatus]]
) -> list[str]:
    calls: list[str] = []
    queue = list(responses)

    def factory(_delay: float):
        jobs, status = queue.pop(0)
        collector = _Collector(jobs, status)
        original = collector.collect

        def collect(target):
            calls.append(target.board_id)
            return original(target)

        collector.collect = collect
        return collector, _Counter()

    monkeypatch.setattr(stage_b, "_collector", factory)
    return calls


def _run(manifest: dict[str, object], **overrides):
    values = {
        "max_targets": 5,
        "max_estimated_requests": 10_000,
        "delay": 0.5,
        "include_high_cost": True,
        "include_potentially_capped": True,
        "allow_uncertain_cost": True,
    }
    values.update(overrides)
    return stage_b.run(manifest, **values)


def _unique_jobs(count: int) -> list[Job]:
    jobs = []
    for number in range(count):
        job = _job(str(number))
        job.title = f"Support Engineer {number}"
        job.description_text = f"{job.description_text} Position {number}."
        jobs.append(job)
    return jobs


def test_frozen_manifest_preserves_workday_cohort_and_hash():
    manifest = stage_b.load_manifest()
    assert (
        manifest["manifest_sha256"]
        == "054238a3b5e61db43688416b84fcaee01b2dfe685a5e710db07fe61ccbf9396c"
    )
    assert len(manifest["targets"]) == 536
    assert {target["source"] for target in manifest["targets"]} == {"workday"}


def test_bootstrap_uses_read_only_stage_a_and_records_topology(monkeypatch, tmp_path):
    manifest = _stage_a_fixture(monkeypatch, tmp_path)
    source_sha = stage_b.artifact_sha(stage_b.STAGE_A_DATABASE)
    continuity = stage_b.bootstrap(manifest)
    assert stage_b.artifact_sha(stage_b.STAGE_A_DATABASE) == source_sha
    assert continuity["invariants"]["jobs"] == 44
    assert set(continuity["topology_fingerprints"]) == {
        "posting_delivery_groups",
        "delivery_keys",
        "stage_a_delivery_rows",
    }
    assert stage_b.bootstrap(manifest) == continuity


def test_manifest_mismatch_refuses_existing_continuity(monkeypatch, tmp_path):
    manifest = _stage_a_fixture(monkeypatch, tmp_path / "preferred")
    stage_b.bootstrap(manifest)
    current = json.loads(stage_b.CONTINUITY.read_text())
    current["topology_fingerprints"]["delivery_keys"] = "bad"
    stage_b.write_json(stage_b.CONTINUITY, current)
    with pytest.raises(ValueError, match="incompatible"):
        stage_b.bootstrap(manifest)


def test_preflight_is_no_network_no_mutation_and_separates_pause_reasons(monkeypatch, tmp_path):
    manifest = _stage_a_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(stage_b, "_collector", lambda _: pytest.fail("network"))
    output = stage_b.preflight(
        manifest,
        max_targets=1,
        max_estimated_requests=100,
        include_high_cost=False,
        include_potentially_capped=False,
        allow_uncertain_cost=False,
    )
    assert output["current_invocation_pause_reason"] is None
    assert output["planned_pause_reason"] == "invocation_target_limit_reached"
    assert not stage_b.DATABASE.exists()


def test_cross_source_stage_a_delivery_and_historical_link_suppress_stage_b(monkeypatch, tmp_path):
    manifest = _stage_a_fixture(monkeypatch, tmp_path)
    history_url = "https://history.test/0"
    calls = _responses(monkeypatch, [([_job("a", url=history_url)], CollectionStatus.SUCCESS)])
    output = _run(manifest, max_targets=1)
    assert calls and output["delivery"]["stage_b_incremental_fresh"] == 0
    assert output["delivery"]["already_delivered_groups"] == 1


def test_sqlite_abort_inside_terminal_transaction_rolls_back_and_is_retryable(
    monkeypatch, tmp_path
):
    manifest = _stage_a_fixture(monkeypatch, tmp_path)
    calls = _responses(
        monkeypatch,
        [([_job("one")], CollectionStatus.SUCCESS), ([_job("one")], CollectionStatus.SUCCESS)],
    )
    stage_b.bootstrap(manifest)
    with sqlite3.connect(stage_b.DATABASE) as connection:
        connection.execute(
            "CREATE TRIGGER abort_stage_b_delivery "
            "BEFORE INSERT ON stage_b_deliveries "
            "BEGIN SELECT RAISE(ABORT, 'forced delivery failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="forced delivery failure"):
        _run(manifest, max_targets=1)
    with sqlite3.connect(stage_b.DATABASE) as connection:
        target = connection.execute("SELECT state, result_json FROM stage_b_target_runs").fetchone()
        assert target == ("in_progress", None)
        assert connection.execute("SELECT COUNT(*) FROM stage_b_deliveries").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM group_deliveries WHERE job_id=? AND destination=?",
                ("workday-one", stage_b.CUMULATIVE_DESTINATION),
            ).fetchone()[0]
            == 0
        )
        connection.execute("DROP TRIGGER abort_stage_b_delivery")
    _run(manifest, max_targets=1)
    assert len(calls) == 2
    with sqlite3.connect(stage_b.DATABASE) as connection:
        assert connection.execute("SELECT COUNT(*) FROM stage_b_target_runs").fetchone()[0] == 1
        assert (
            connection.execute("SELECT state FROM stage_b_target_runs").fetchone()[0] == "completed"
        )
        assert connection.execute("SELECT COUNT(*) FROM stage_b_deliveries").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM group_deliveries WHERE job_id=? AND destination=?",
                ("workday-one", stage_b.CUMULATIVE_DESTINATION),
            ).fetchone()[0]
            == 1
        )
    assert stage_b.summarize(manifest)["capacity"]["stage_b_incremental_fresh"] == 1


def test_delivery_table_not_result_json_is_authoritative_for_capacity(monkeypatch, tmp_path):
    manifest = _stage_a_fixture(monkeypatch, tmp_path)
    _responses(monkeypatch, [([_job("one")], CollectionStatus.SUCCESS)])
    _run(manifest, max_targets=1)
    with sqlite3.connect(stage_b.DATABASE) as connection:
        row = connection.execute("SELECT result_json FROM stage_b_target_runs").fetchone()
        result = json.loads(row[0])
        result["fresh_unique_deliveries"] = 999
        result["deliveries"] = [{"decision": "possible_match"}] * 999
        connection.execute("UPDATE stage_b_target_runs SET result_json=?", (json.dumps(result),))
        decisions = {
            decision: count
            for decision, count in connection.execute(
                "SELECT decision, COUNT(*) FROM stage_b_deliveries GROUP BY decision"
            )
        }
    summary = stage_b.summarize(manifest)
    assert summary["targets"]["success"] == 1
    assert summary["delivery"]["stage_b_incremental_fresh"] == 1
    assert summary["capacity"]["combined_fresh"] == 45
    assert summary["delivery"]["stage_b_incremental_strong"] == decisions.get("strong_match", 0)
    assert summary["delivery"]["stage_b_incremental_review"] == decisions.get("possible_match", 0)


def test_post_commit_crash_does_not_repeat_target_or_duplicate_delivery(monkeypatch, tmp_path):
    manifest = _stage_a_fixture(monkeypatch, tmp_path)
    calls = _responses(monkeypatch, [([_job("one")], CollectionStatus.SUCCESS)])
    original = stage_b.regenerate_csv
    monkeypatch.setattr(
        stage_b,
        "regenerate_csv",
        lambda _: (_ for _ in ()).throw(RuntimeError("crash after commit")),
    )
    with pytest.raises(RuntimeError, match="after commit"):
        _run(manifest, max_targets=1)
    assert stage_b.completed_values(manifest)[0]["fresh_unique_deliveries"] == 1
    monkeypatch.setattr(stage_b, "regenerate_csv", original)
    _run(manifest, max_targets=1)
    assert len(calls) == 1
    assert stage_b.regenerate_csv(manifest) == 1


def test_stage_b_repeat_and_csv_regeneration_are_idempotent(monkeypatch, tmp_path):
    manifest = _stage_a_fixture(monkeypatch, tmp_path)
    manifest["targets"] = [_target(1), _target(2)]
    calls = _responses(
        monkeypatch,
        [([_job("same")], CollectionStatus.SUCCESS), ([_job("same")], CollectionStatus.SUCCESS)],
    )
    _run(manifest, max_targets=2)
    assert len(calls) == 2
    assert [value["fresh_unique_deliveries"] for value in stage_b.completed_values(manifest)] == [
        1,
        0,
    ]
    stage_b.INCREMENTAL_DELIVERIES.write_text("corrupt\n")
    assert stage_b.regenerate_csv(manifest) == 1
    first = stage_b.INCREMENTAL_DELIVERIES.read_bytes()
    assert stage_b.regenerate_csv(manifest) == 1
    assert stage_b.INCREMENTAL_DELIVERIES.read_bytes() == first


def test_business_200_is_not_terminal_and_preferred_250_stops_before_later_target(
    monkeypatch, tmp_path
):
    manifest = _stage_a_fixture(monkeypatch, tmp_path)
    manifest["targets"] = [_target(1), _target(2)]
    many = _unique_jobs(156)
    calls = _responses(
        monkeypatch, [(many, CollectionStatus.SUCCESS), ([_job("later")], CollectionStatus.SUCCESS)]
    )
    _run(manifest, max_targets=2)
    assert len(calls) == 2  # combined 200 after first did not stop the invocation
    manifest = _stage_a_fixture(monkeypatch, tmp_path / "preferred")
    manifest["targets"] = [_target(1), _target(2)]
    calls = _responses(monkeypatch, [(_unique_jobs(206), CollectionStatus.SUCCESS)])
    result = _run(manifest, max_targets=2)
    assert result["terminal_stop_reason"] == "combined_preferred_threshold_reached"
    assert len(calls) == 1
    assert result["targets"]["remaining"] == 1


def test_terminal_all_targets_only_after_every_target_is_completed(monkeypatch, tmp_path):
    manifest = _stage_a_fixture(monkeypatch, tmp_path)
    manifest["targets"] = [_target(1), _target(2)]
    _responses(
        monkeypatch,
        [([_job("one")], CollectionStatus.PARTIAL), ([_job("two")], CollectionStatus.SUCCESS)],
    )
    first = _run(manifest, max_targets=1)
    assert first["terminal_stop_reason"] is None
    assert first["targets"]["partial"] == 1
    second = _run(manifest, max_targets=1)
    assert second["terminal_stop_reason"] == "all_stage_b_targets_completed"


def test_deferred_high_cost_target_is_not_reported_as_all_complete(monkeypatch, tmp_path):
    manifest = _stage_a_fixture(monkeypatch, tmp_path)
    manifest["targets"] = [_target(1), _target(2, tier="exact_500_plus", estimate=500)]
    _responses(monkeypatch, [([_job("one")], CollectionStatus.SUCCESS)])
    result = _run(manifest, max_targets=5, include_high_cost=False)
    assert result["terminal_stop_reason"] is None
    assert result["targets"]["remaining"] == 1
    assert result["targets"]["pending_deferred_high_cost"] == 1


def test_systemic_pause_is_durable_and_next_resume_runs_then_success_resets(monkeypatch, tmp_path):
    manifest = _stage_a_fixture(monkeypatch, tmp_path)
    manifest["targets"] = [_target(number) for number in range(5)]
    calls = _responses(
        monkeypatch,
        [
            ([], CollectionStatus.NETWORK_FAILURE),
            ([], CollectionStatus.NETWORK_FAILURE),
            ([], CollectionStatus.NETWORK_FAILURE),
            ([_job("ok")], CollectionStatus.SUCCESS),
        ],
    )
    first = _run(manifest, max_targets=4)
    assert [value["collection_status"] for value in stage_b.completed_values(manifest)] == [
        "network_failure",
        "network_failure",
        "network_failure",
    ]
    assert first["invocation_pause_reason"] == "systemic_provider_failure_pause", first
    status = stage_b.preflight(
        manifest,
        max_targets=1,
        max_estimated_requests=100,
        include_high_cost=True,
        include_potentially_capped=True,
        allow_uncertain_cost=True,
    )
    assert status["current_invocation_pause_reason"] == "systemic_provider_failure_pause"
    second = _run(manifest, max_targets=1)
    assert len(calls) == 4
    assert second["invocation_pause_reason"] == "invocation_target_limit_reached"
