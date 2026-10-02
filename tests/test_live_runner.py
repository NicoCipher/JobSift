from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from job_scout import live_runner
from job_scout.live_runner import (
    LiveRunnerConfig,
    _candidate_job_ids,
    _discard_stale_unpublished_release,
)
from job_scout.sourcing_plan import SourcingPlan
from job_scout.storage.daily_batches import DailyBatchStore
from job_scout.storage.sqlite import SQLiteRepository


def test_stale_unpublished_release_can_be_discarded(tmp_path):
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")
    store = DailyBatchStore(repository)
    with repository.connect() as connection:
        connection.execute(
            "INSERT INTO daily_batches "
            "(batch_id,client_id,destination,idempotency_key,requested_quota,"
            "selected_count,shortfall,status,assembled_at,request_json,counts_json,"
            "dedupe_version,error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "stale-unpublished",
                "client-a",
                "client-sheet://jobs",
                "scope-1",
                1,
                0,
                1,
                "failed",
                datetime.now(UTC).isoformat(),
                "{}",
                "{}",
                "test",
                "prepared posting is no longer fresh at delivery: stale_posting",
            ),
        )

    batch = store.get("stale-unpublished")
    assert _discard_stale_unpublished_release(store, batch) is True
    with pytest.raises(Exception, match="batch not found"):
        store.get("stale-unpublished")


def test_stale_release_with_export_journal_is_never_auto_discarded(tmp_path):
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")
    store = DailyBatchStore(repository)
    with repository.connect() as connection:
        connection.execute(
            "INSERT INTO daily_batches "
            "(batch_id,client_id,destination,idempotency_key,requested_quota,"
            "selected_count,shortfall,status,assembled_at,request_json,counts_json,"
            "dedupe_version,export_before_sha256,export_after_sha256,error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "stale-journaled",
                "client-a",
                "client-sheet://jobs",
                "scope-1",
                1,
                0,
                1,
                "failed",
                datetime.now(UTC).isoformat(),
                "{}",
                "{}",
                "test",
                "a" * 64,
                "b" * 64,
                "prepared posting is no longer fresh at delivery: stale_posting",
            ),
        )

    batch = store.get("stale-journaled")
    assert _discard_stale_unpublished_release(store, batch) is False
    assert store.get("stale-journaled").status == "failed"


def test_unresolved_batch_ignores_failed_unpublished_snapshot(tmp_path):
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")
    DailyBatchStore(repository)
    with repository.connect() as connection:
        connection.execute(
            "INSERT INTO daily_batches "
            "(batch_id,client_id,destination,idempotency_key,requested_quota,"
            "selected_count,shortfall,status,assembled_at,request_json,counts_json,"
            "dedupe_version,error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "failed-unpublished",
                "client-a",
                "client-sheet://jobs",
                "scope-1",
                1,
                0,
                1,
                "failed",
                datetime.now(UTC).isoformat(),
                "{}",
                "{}",
                "test",
                "posting expired before release",
            ),
        )

    assert (
        live_runner._unresolved_batch(
            repository,
            client_id="client-a",
            destination="client-sheet://jobs",
        )
        is None
    )


def test_unresolved_batch_ignores_delivered_export_journal(tmp_path):
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")
    DailyBatchStore(repository)
    with repository.connect() as connection:
        connection.execute(
            "INSERT INTO daily_batches "
            "(batch_id,client_id,destination,idempotency_key,requested_quota,"
            "selected_count,shortfall,status,assembled_at,delivered_at,request_json,"
            "counts_json,dedupe_version,export_after_sha256) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "delivered-batch",
                "client-a",
                "client-sheet://jobs",
                "scope-1",
                1,
                0,
                1,
                "delivered",
                datetime.now(UTC).isoformat(),
                datetime.now(UTC).isoformat(),
                "{}",
                "{}",
                "test",
                "a" * 64,
            ),
        )

    assert (
        live_runner._unresolved_batch(
            repository,
            client_id="client-a",
            destination="client-sheet://jobs",
        )
        is None
    )


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
            stale_posting_suppressed_groups=0,
            unknown_age_suppressed_groups=0,
            invalid_time_suppressed_groups=0,
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
    monkeypatch.setattr(
        live_runner,
        "ClientSheetDestinationStore",
        lambda _: SimpleNamespace(list=lambda client_id: ()),
    )
    monkeypatch.setattr(live_runner, "sheet_destination", lambda *_: "gsheet://sheet123/Sheet1")
    monkeypatch.setattr(live_runner, "_unresolved_batch", lambda *_, **__: batch)

    def should_not_source(*args, **kwargs):
        raise AssertionError("sourcing must not run while a batch awaits release")

    monkeypatch.setattr(live_runner, "collect_inventory_plan", should_not_source)

    result = live_runner.run_once(config)

    assert result["action"] == "awaiting_release"
    assert result["batch_id"] == "batch-1"

def test_sole_ready_client_destination_is_auto_selected(tmp_path, monkeypatch):
    config = LiveRunnerConfig(
        plan_path=tmp_path / "plan.json",
        database_path=tmp_path / "jobs.sqlite3",
        csv_path=tmp_path / "jobs.csv",
        reports_dir=tmp_path / "runs",
        spreadsheet_id="legacy-sheet",
        sheet_tab="Legacy",
        quota=5,
        interval_seconds=86400,
        timezone="Africa/Lagos",
        auto_release=True,
        allow_partial=False,
        run_once=True,
    )
    destination = SimpleNamespace(
        destination_id="primary-jobs",
        logical_uri="client-sheet://primary-jobs",
        config_sha256="a" * 64,
        status="ready",
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
        live_runner, "SQLiteRepository", lambda _: SimpleNamespace(remote_url="")
    )
    monkeypatch.setattr(live_runner, "DailyBatchStore", lambda _: SimpleNamespace())
    monkeypatch.setattr(
        live_runner,
        "ClientSheetDestinationStore",
        lambda _: SimpleNamespace(list=lambda client_id: (destination,)),
    )
    observed = {}

    def unresolved(repository, *, client_id, destination):
        observed["destination"] = destination

    monkeypatch.setattr(live_runner, "_unresolved_batch", unresolved)

    result = live_runner.run_once(config)

    assert observed["destination"] == "client-sheet://primary-jobs"
    assert result["action"] == "nothing_to_release"
    assert result["destination"] == "client-sheet://primary-jobs"


def test_multiple_ready_client_destinations_require_explicit_selection(tmp_path, monkeypatch):
    config = LiveRunnerConfig(
        plan_path=tmp_path / "plan.json",
        database_path=tmp_path / "jobs.sqlite3",
        csv_path=tmp_path / "jobs.csv",
        reports_dir=tmp_path / "runs",
        spreadsheet_id="legacy-sheet",
        sheet_tab="Legacy",
        quota=5,
        interval_seconds=86400,
        timezone="Africa/Lagos",
        auto_release=False,
        allow_partial=False,
        run_once=True,
    )
    destinations = tuple(
        SimpleNamespace(
            destination_id=value,
            logical_uri=f"client-sheet://{value}",
            config_sha256="a" * 64,
            status="ready",
        )
        for value in ("primary", "secondary")
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
        live_runner, "SQLiteRepository", lambda _: SimpleNamespace(remote_url="")
    )
    monkeypatch.setattr(live_runner, "DailyBatchStore", lambda _: SimpleNamespace())
    monkeypatch.setattr(
        live_runner,
        "ClientSheetDestinationStore",
        lambda _: SimpleNamespace(list=lambda client_id: destinations),
    )

    with pytest.raises(ValueError, match="multiple ready client destinations"):
        live_runner.run_once(config)


def test_registered_client_destination_replaces_global_sheet_coordinates(tmp_path, monkeypatch):
    config = LiveRunnerConfig(
        plan_path=tmp_path / "plan.json",
        database_path=tmp_path / "jobs.sqlite3",
        csv_path=tmp_path / "jobs.csv",
        reports_dir=tmp_path / "runs",
        spreadsheet_id=None,
        sheet_tab=None,
        destination_id="primary-jobs",
        quota=5,
        interval_seconds=86400,
        timezone="Africa/Lagos",
        auto_release=True,
        allow_partial=False,
        run_once=True,
    )
    destination = SimpleNamespace(
        destination_id="primary-jobs",
        logical_uri="client-sheet://primary-jobs",
        config_sha256="a" * 64,
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
        live_runner, "SQLiteRepository", lambda _: SimpleNamespace(remote_url="")
    )
    monkeypatch.setattr(live_runner, "DailyBatchStore", lambda _: SimpleNamespace())
    monkeypatch.setattr(
        live_runner,
        "ClientSheetDestinationStore",
        lambda _: SimpleNamespace(
            get=lambda client_id, destination_id: destination
        ),
    )

    observed = {}

    def unresolved(repository, *, client_id, destination):
        observed["client_id"] = client_id
        observed["destination"] = destination

    monkeypatch.setattr(live_runner, "_unresolved_batch", unresolved)

    result = live_runner.run_once(config)

    assert observed == {
        "client_id": "client",
        "destination": "client-sheet://primary-jobs",
    }
    assert result["action"] == "nothing_to_release"
    assert result["destination"] == "client-sheet://primary-jobs"


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
    monkeypatch.setattr(
        live_runner,
        "ClientSheetDestinationStore",
        lambda _: SimpleNamespace(list=lambda client_id: ()),
    )
    monkeypatch.setattr(live_runner, "sheet_destination", lambda *_: "gsheet://sheet123/Sheet1")
    monkeypatch.setattr(live_runner, "_unresolved_batch", lambda *_, **__: None)

    def should_not_source(*args, **kwargs):
        raise AssertionError("release mode must not source a replacement batch")

    monkeypatch.setattr(live_runner, "collect_inventory_plan", should_not_source)

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
    monkeypatch.setattr(
        live_runner,
        "ClientSheetDestinationStore",
        lambda _: SimpleNamespace(list=lambda client_id: ()),
    )
    monkeypatch.setattr(live_runner, "sheet_destination", lambda *_: "gsheet://sheet123/Sheet1")
    monkeypatch.setattr(live_runner, "_unresolved_batch", lambda *_, **__: batch)

    def should_not_source(*args, **kwargs):
        raise AssertionError("discard mode must not source a replacement batch")

    monkeypatch.setattr(live_runner, "collect_inventory_plan", should_not_source)

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


def test_retention_runs_before_client_evaluation(tmp_path, monkeypatch):
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
    events = []
    repository = SimpleNamespace(remote_url="")

    def prune(**kwargs):
        events.append("prune")
        return {"deleted_jobs": 0, "compacted_jobs": 1}

    repository.prune_stale_inventory = prune
    store = SimpleNamespace(evidence_digest=lambda *_, **__: "a" * 64)
    plan = SimpleNamespace(plan_id="pilot")
    brief = SimpleNamespace(
        client_id="client",
        delivery_policy=SimpleNamespace(
            max_jobs_per_employer_per_batch=1,
            employer_cooldown_days=0,
        ),
        posting_freshness=SimpleNamespace(max_age_hours=24, unknown_policy="reject"),
    )
    report = SimpleNamespace(
        run_id="inventory-run-1",
        status="success",
        started_at=datetime(2026, 9, 30, 9, 0, tzinfo=UTC),
        completed_at=datetime(2026, 9, 30, 9, 1, tzinfo=UTC),
        targets=[],
    )
    evaluation = SimpleNamespace(
        evaluated_at=report.completed_at,
        total_matched=1,
        total_rejected=0,
    )

    monkeypatch.setattr(live_runner, "_runtime_plan", lambda _: (plan, brief_path))
    monkeypatch.setattr(live_runner, "load_search_brief", lambda _: brief)
    monkeypatch.setattr(live_runner, "SQLiteRepository", lambda _: repository)
    monkeypatch.setattr(live_runner, "DailyBatchStore", lambda _: store)
    monkeypatch.setattr(live_runner, "sheet_destination", lambda *_: "gsheet://sheet123/Sheet1")
    monkeypatch.setattr(live_runner, "_unresolved_batch", lambda *_, **__: None)
    monkeypatch.setattr(live_runner, "_batch_by_idempotency", lambda *_, **__: None)
    monkeypatch.setattr(live_runner, "collect_inventory_plan", lambda *_, **__: report)

    def evaluate(**kwargs):
        assert events == []
        assert kwargs["retention_hours"] == 72
        events.append("evaluate")
        return evaluation

    monkeypatch.setattr(live_runner, "evaluate_inventory_run", evaluate)
    monkeypatch.setattr(
        live_runner,
        "InventoryRunStore",
        lambda _: SimpleNamespace(
            active_job_ids=lambda run_id, **kwargs: ("job-1",)
        ),
    )
    monkeypatch.setattr(live_runner, "_source_failures", lambda _: ())
    monkeypatch.setattr(live_runner, "prepare_daily_batch", lambda **_: object())
    monkeypatch.setattr(
        live_runner,
        "_batch_payload",
        lambda store, batch, *, action, sourcing=None, evaluation=None, retention=None: {
            "action": action
        },
    )

    assert live_runner.run_once(config) == {"action": "validated"}
    assert events == ["evaluate"]


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
    store = SimpleNamespace(evidence_digest=lambda *_, **__: "a" * 64)
    plan = SimpleNamespace(plan_id="pilot")
    brief = SimpleNamespace(
        client_id="client",
        delivery_policy=SimpleNamespace(
            max_jobs_per_employer_per_batch=1,
            employer_cooldown_days=0,
        ),
        posting_freshness=SimpleNamespace(
            max_age_hours=24,
            unknown_policy="reject",
        ),
    )
    report = SimpleNamespace(
        run_id="inventory-run-1",
        status="success",
        started_at=datetime(2026, 9, 30, 9, 0, tzinfo=UTC),
        completed_at=datetime(2026, 9, 30, 9, 1, tzinfo=UTC),
        targets=[],
    )
    evaluation = SimpleNamespace(
        evaluated_at=report.completed_at,
        total_matched=1,
        total_rejected=0,
    )

    monkeypatch.setattr(live_runner, "_runtime_plan", lambda _: (plan, brief_path))
    monkeypatch.setattr(live_runner, "load_search_brief", lambda _: brief)
    monkeypatch.setattr(live_runner, "SQLiteRepository", lambda _: repository)
    monkeypatch.setattr(live_runner, "DailyBatchStore", lambda _: store)
    monkeypatch.setattr(live_runner, "sheet_destination", lambda *_: "gsheet://sheet123/Sheet1")
    monkeypatch.setattr(live_runner, "_unresolved_batch", lambda *_, **__: None)
    monkeypatch.setattr(live_runner, "_batch_by_idempotency", lambda *_, **__: None)
    monkeypatch.setattr(live_runner, "collect_inventory_plan", lambda *_, **__: report)
    monkeypatch.setattr(
        live_runner, "evaluate_inventory_run", lambda *_, **__: evaluation
    )
    monkeypatch.setattr(
        live_runner,
        "InventoryRunStore",
        lambda _: SimpleNamespace(active_job_ids=lambda run_id, **kwargs: ("job-1",)),
    )
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
        lambda store, batch, *, action, sourcing=None, evaluation=None, retention=None: {
            "action": action,
            "retention": retention,
        },
    )

    assert live_runner.run_once(config) == {
        "action": "validated",
        "retention": None,
    }

