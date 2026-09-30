from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from job_scout import live_runner
from job_scout.live_runner import LiveRunnerConfig, _candidate_job_ids
from job_scout.sourcing_plan import SourcingPlan
from job_scout.storage.sqlite import SQLiteRepository


def test_candidate_job_ids_are_scoped_to_current_run_and_targets(tmp_path):
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")
    plan = SourcingPlan.model_validate(
        {
            "plan_version": "sourcing-plan-v1",
            "plan_id": "pilot",
            "search_brief": "brief.json",
            "database": "jobs.sqlite3",
            "csv": "jobs.csv",
            "targets": [
                {"source": "greenhouse", "company": "GitLab", "board": "gitlab"},
                {"source": "ashby", "company": "Render", "board": "render"},
            ],
        }
    )
    with repository.connect() as connection:
        for job_id, source, board in (
            ("in-greenhouse", "greenhouse", "gitlab"),
            ("in-ashby", "ashby", "render"),
            ("wrong-board", "greenhouse", "other"),
            ("too-old", "greenhouse", "gitlab"),
        ):
            connection.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    source,
                    job_id,
                    board,
                    f"https://example.com/{job_id}",
                    job_id,
                    "2026-09-29T00:00:00+00:00",
                    "2026-09-29T00:00:00+00:00",
                    "2026-09-29T00:00:00+00:00",
                    "new",
                    "{}",
                ),
            )
        for job_id, evaluated_at in (
            ("in-greenhouse", "2026-09-29T12:01:00+00:00"),
            ("in-ashby", "2026-09-29T12:02:00+00:00"),
            ("wrong-board", "2026-09-29T12:03:00+00:00"),
            ("too-old", "2026-09-29T11:59:00+00:00"),
        ):
            connection.execute(
                "INSERT INTO job_matches VALUES (?,?,?,?,?,?,?,?)",
                (job_id, "client", "strong_match", None, "[]", "[]", evaluated_at, "test"),
            )

    assert _candidate_job_ids(
        repository,
        client_id="client",
        plan=plan,
        started_at=datetime(2026, 9, 29, 12, 0, tzinfo=UTC),
        completed_at=datetime(2026, 9, 29, 12, 5, tzinfo=UTC),
    ) == ("in-ashby", "in-greenhouse")


def test_unresolved_batch_blocks_new_sourcing(tmp_path, monkeypatch):
    config = LiveRunnerConfig(
        plan_path=tmp_path / "plan.json",
        database_path=tmp_path / "jobs.sqlite3",
        csv_path=tmp_path / "jobs.csv",
        reports_dir=tmp_path / "runs",
        spreadsheet_id="sheet123",
        sheet_tab="Sheet1",
        quota=5,
        interval_seconds=86400,
        timezone="Africa/Lagos",
        auto_release=False,
        allow_partial=False,
        run_once=True,
    )
    request = SimpleNamespace(requested_quota=5, completeness="complete")
    batch = SimpleNamespace(
        batch_id="batch-1",
        status="prepared",
        request=request,
        selected_count=2,
        shortfall=3,
        error=None,
        counts=SimpleNamespace(
            fresh_eligible_employers=2,
            company_cap_suppressed_groups=0,
            employer_cooldown_suppressed_groups=0,
        ),
    )
    store = SimpleNamespace(
        export_rows=lambda batch_id: [
            {
                "Job Title": "Software Engineer",
                "Company Name": "Example",
                "Job Link": "https://example.com/job",
                "Job Platform": "greenhouse",
            }
        ]
    )
    monkeypatch.setattr(
        live_runner,
        "_runtime_plan",
        lambda _: (SimpleNamespace(plan_id="pilot"), tmp_path / "brief.json"),
    )
    monkeypatch.setattr(
        live_runner, "load_search_brief", lambda _: SimpleNamespace(client_id="client")
    )
    monkeypatch.setattr(live_runner, "SQLiteRepository", lambda _: object())
    monkeypatch.setattr(live_runner, "DailyBatchStore", lambda _: store)
    monkeypatch.setattr(live_runner, "sheet_destination", lambda *_: "gsheet://sheet123/Sheet1")
    monkeypatch.setattr(live_runner, "_unresolved_batch", lambda *_, **__: batch)

    def should_not_source(*args, **kwargs):
        raise AssertionError("sourcing must not run while a batch awaits release")

    monkeypatch.setattr(live_runner, "run_sourcing_plan", should_not_source)

    result = live_runner.run_once(config)

    assert result["action"] == "awaiting_release"
    assert result["batch_id"] == "batch-1"

def test_release_mode_never_sources_without_prepared_batch(tmp_path, monkeypatch):
    config = LiveRunnerConfig(
        plan_path=tmp_path / "plan.json",
        database_path=tmp_path / "jobs.sqlite3",
        csv_path=tmp_path / "jobs.csv",
        reports_dir=tmp_path / "runs",
        spreadsheet_id="sheet123",
        sheet_tab="Sheet1",
        quota=5,
        interval_seconds=86400,
        timezone="Africa/Lagos",
        auto_release=True,
        allow_partial=False,
        run_once=True,
    )
    monkeypatch.setattr(
        live_runner,
        "_runtime_plan",
        lambda _: (SimpleNamespace(plan_id="pilot"), tmp_path / "brief.json"),
    )
    monkeypatch.setattr(
        live_runner, "load_search_brief", lambda _: SimpleNamespace(client_id="client")
    )
    monkeypatch.setattr(live_runner, "SQLiteRepository", lambda _: object())
    monkeypatch.setattr(
        live_runner, "DailyBatchStore", lambda _: SimpleNamespace()
    )
    monkeypatch.setattr(live_runner, "sheet_destination", lambda *_: "gsheet://sheet123/Sheet1")
    monkeypatch.setattr(live_runner, "_unresolved_batch", lambda *_, **__: None)

    def should_not_source(*args, **kwargs):
        raise AssertionError("release mode must not source a replacement batch")

    monkeypatch.setattr(live_runner, "run_sourcing_plan", should_not_source)

    result = live_runner.run_once(config)

    assert result["action"] == "nothing_to_release"
    assert result["plan_id"] == "pilot"

def test_discard_mode_removes_unreleased_batch_without_sourcing(tmp_path, monkeypatch):
    config = LiveRunnerConfig(
        plan_path=tmp_path / "plan.json",
        database_path=tmp_path / "jobs.sqlite3",
        csv_path=tmp_path / "jobs.csv",
        reports_dir=tmp_path / "runs",
        spreadsheet_id="sheet123",
        sheet_tab="Sheet1",
        quota=5,
        interval_seconds=86400,
        timezone="Africa/Lagos",
        auto_release=False,
        allow_partial=False,
        run_once=True,
        discard_prepared=True,
    )
    batch = SimpleNamespace(batch_id="batch-1", selected_count=5)
    store = SimpleNamespace(discard_prepared=lambda batch_id: batch)
    monkeypatch.setattr(
        live_runner,
        "_runtime_plan",
        lambda _: (SimpleNamespace(plan_id="pilot"), tmp_path / "brief.json"),
    )
    monkeypatch.setattr(
        live_runner, "load_search_brief", lambda _: SimpleNamespace(client_id="client")
    )
    monkeypatch.setattr(live_runner, "SQLiteRepository", lambda _: object())
    monkeypatch.setattr(live_runner, "DailyBatchStore", lambda _: store)
    monkeypatch.setattr(live_runner, "sheet_destination", lambda *_: "gsheet://sheet123/Sheet1")
    monkeypatch.setattr(live_runner, "_unresolved_batch", lambda *_, **__: batch)

    def should_not_source(*args, **kwargs):
        raise AssertionError("discard mode must not source a replacement batch")

    monkeypatch.setattr(live_runner, "run_sourcing_plan", should_not_source)

    result = live_runner.run_once(config)

    assert result["action"] == "discarded_prepared"
    assert result["batch_id"] == "batch-1"
    assert result["selected_count"] == 5

def test_validation_mode_refuses_cloud_repository(tmp_path, monkeypatch):
    config = LiveRunnerConfig(
        plan_path=tmp_path / "plan.json",
        database_path=tmp_path / "validation.sqlite3",
        csv_path=tmp_path / "jobs.csv",
        reports_dir=tmp_path / "runs",
        spreadsheet_id="sheet123",
        sheet_tab="Sheet1",
        quota=5,
        interval_seconds=86400,
        timezone="Africa/Lagos",
        auto_release=False,
        allow_partial=False,
        run_once=True,
        validation_only=True,
    )
    monkeypatch.setattr(
        live_runner,
        "_runtime_plan",
        lambda _: (SimpleNamespace(plan_id="pilot"), tmp_path / "brief.json"),
    )
    monkeypatch.setattr(
        live_runner, "load_search_brief", lambda _: SimpleNamespace(client_id="client")
    )
    monkeypatch.setattr(
        live_runner,
        "SQLiteRepository",
        lambda _: SimpleNamespace(remote_url="libsql://production"),
    )

    with pytest.raises(ValueError, match="local-only repository"):
        live_runner.run_once(config)


def test_validation_mode_sources_locally_but_never_releases(tmp_path, monkeypatch):
    brief_path = tmp_path / "brief.json"
    brief_path.write_text("{}", encoding="utf-8")
    config = LiveRunnerConfig(
        plan_path=tmp_path / "plan.json",
        database_path=tmp_path / "validation.sqlite3",
        csv_path=tmp_path / "jobs.csv",
        reports_dir=tmp_path / "runs",
        spreadsheet_id="sheet123",
        sheet_tab="Sheet1",
        quota=5,
        interval_seconds=86400,
        timezone="Africa/Lagos",
        auto_release=False,
        allow_partial=False,
        run_once=True,
        validation_only=True,
    )
    repository = SimpleNamespace(
        remote_url="",
        prune_stale_inventory=lambda **_: {"deleted_jobs": 0, "compacted_jobs": 0},
    )
    store = SimpleNamespace(evidence_digest=lambda *_: "a" * 64)
    plan = SimpleNamespace(plan_id="pilot")
    brief = SimpleNamespace(
        client_id="client",
        delivery_policy=SimpleNamespace(
            max_jobs_per_employer_per_batch=1,
            employer_cooldown_days=0,
        ),
    )
    report = SimpleNamespace(
        status="success",
        started_at=datetime(2026, 9, 30, 9, 0, tzinfo=UTC),
        completed_at=datetime(2026, 9, 30, 9, 1, tzinfo=UTC),
        targets=[],
    )

    monkeypatch.setattr(live_runner, "_runtime_plan", lambda _: (plan, brief_path))
    monkeypatch.setattr(live_runner, "load_search_brief", lambda _: brief)
    monkeypatch.setattr(live_runner, "SQLiteRepository", lambda _: repository)
    monkeypatch.setattr(live_runner, "DailyBatchStore", lambda _: store)
    monkeypatch.setattr(live_runner, "sheet_destination", lambda *_: "gsheet://sheet123/Sheet1")
    monkeypatch.setattr(live_runner, "_unresolved_batch", lambda *_, **__: None)
    monkeypatch.setattr(live_runner, "_batch_by_idempotency", lambda *_, **__: None)
    monkeypatch.setattr(live_runner, "run_sourcing_plan", lambda *_, **__: report)
    monkeypatch.setattr(live_runner, "_candidate_job_ids", lambda *_, **__: ("job-1",))
    monkeypatch.setattr(live_runner, "_source_failures", lambda _: ())
    monkeypatch.setattr(live_runner, "prepare_daily_batch", lambda **_: object())
    monkeypatch.setattr(
        live_runner,
        "finalize_daily_batch",
        lambda **_: (_ for _ in ()).throw(AssertionError("validation must never release")),
    )
    monkeypatch.setattr(
        live_runner,
        "_batch_payload",
        lambda store, batch, *, action, sourcing=None, retention=None: {
            "action": action,
            "retention": retention,
        },
    )

    assert live_runner.run_once(config) == {
        "action": "validated",
        "retention": {"deleted_jobs": 0, "compacted_jobs": 0},
    }

