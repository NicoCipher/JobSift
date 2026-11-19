from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from job_scout import live_runner
from job_scout.live_runner import (
    LiveRunnerConfig,
    _candidate_job_ids,
)
from job_scout.sourcing_plan import SourcingPlan
from job_scout.storage.daily_batches import DailyBatchStore
from job_scout.storage.sqlite import SQLiteRepository


def test_batch_payload_combines_match_funnel_with_delivery_suppressions():
    batch = SimpleNamespace(
        batch_id="batch-funnel",
        status="prepared",
        request=SimpleNamespace(
            destination_id="jobs",
            requested_quota=100,
            completeness="complete",
        ),
        selected_count=1,
        shortfall=99,
        error=None,
        counts=SimpleNamespace(
            fresh_eligible_employers=1,
            match_eligible_postings=30,
            needs_review_postings=2,
            selection_eligible_postings=32,
            historically_suppressed_groups=3,
            previously_delivered_groups=4,
            duplicate_postings_collapsed=5,
            fresh_eligible_groups=20,
            company_cap_suppressed_groups=6,
            employer_cooldown_suppressed_groups=7,
            stale_posting_suppressed_groups=8,
            unknown_age_suppressed_groups=9,
            invalid_time_suppressed_groups=10,
        ),
    )
    evaluation = SimpleNamespace(
        funnel=SimpleNamespace(
            model_dump=lambda **_: {
                "overall": {"fresh_0_24h": 50},
                "by_source": {"greenhouse": {"fresh_0_24h": 30}},
            }
        )
    )
    store = SimpleNamespace(export_rows=lambda _batch_id: [])

    payload = live_runner._batch_payload(
        store,
        batch,
        action="prepared",
        evaluation=evaluation,
    )

    assert payload["client_funnel"]["overall"]["fresh_0_24h"] == 50
    assert payload["client_funnel"]["delivery"] == {
        "historically_suppressed_groups": 3,
        "previously_delivered_groups": 4,
        "duplicate_postings_collapsed": 5,
        "fresh_eligible_groups": 20,
        "fresh_eligible_employers": 1,
        "employer_cooldown_suppressed_groups": 7,
        "stale_posting_suppressed_groups": 8,
        "unknown_age_suppressed_groups": 9,
        "invalid_time_suppressed_groups": 10,
        "company_cap_suppressed_groups": 6,
        "selected_count": 1,
        "shortfall": 99,
    }


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


def test_recovery_state_machine_discards_stale_then_returns_newer_prepared(tmp_path, monkeypatch):
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")
    store = DailyBatchStore(repository)
    with repository.connect() as connection:
        for batch_id, key, status, error, assembled_at in (
            (
                "stale-profile-scope",
                "2026-10-02:scope-old",
                "failed",
                "prepared posting is no longer fresh at delivery: stale_posting",
                "2026-10-02T12:00:00+00:00",
            ),
            (
                "newer-prepared",
                "2026-10-02:scope-new",
                "prepared",
                None,
                "2026-10-02T12:01:00+00:00",
            ),
        ):
            connection.execute(
                "INSERT INTO daily_batches "
                "(batch_id,client_id,destination,idempotency_key,requested_quota,"
                "selected_count,shortfall,status,assembled_at,request_json,counts_json,"
                "dedupe_version,error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    batch_id, "client-a", "client-sheet://jobs", key, 1, 0, 1,
                    status, assembled_at, "{}", "{}", "test", error,
                ),
            )

    monkeypatch.setattr(
        DailyBatchStore,
        "_load",
        staticmethod(lambda _c, batch_id: SimpleNamespace(batch_id=batch_id)),
    )
    result = store.recover_unresolved("client-a", "client-sheet://jobs")
    assert result.batch_id == "newer-prepared"
    with repository.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM daily_batches WHERE batch_id='stale-profile-scope'"
        ).fetchone() is None


def test_recovery_state_machine_retains_journaled_stale_failure(tmp_path, monkeypatch):
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")
    store = DailyBatchStore(repository)
    with repository.connect() as connection:
        connection.execute(
            "INSERT INTO daily_batches "
            "(batch_id,client_id,destination,idempotency_key,requested_quota,"
            "selected_count,shortfall,status,assembled_at,request_json,counts_json,"
            "dedupe_version,error,export_before_sha256) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "journaled-stale",
                "client-a",
                "client-sheet://jobs",
                "scope-journaled",
                1,
                0,
                1,
                "failed",
                "2026-10-02T12:00:00+00:00",
                "{}",
                "{}",
                "test",
                "prepared posting is no longer fresh at delivery: stale_posting",
                "a" * 64,
            ),
        )

    monkeypatch.setattr(
        DailyBatchStore,
        "_load",
        staticmethod(
            lambda _c, batch_id: SimpleNamespace(
                batch_id=batch_id,
                status="failed",
                error="prepared posting is no longer fresh at delivery: stale_posting",
            )
        ),
    )
    result = store.recover_unresolved("client-a", "client-sheet://jobs")
    assert result.batch_id == "journaled-stale"
    with repository.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM daily_batches WHERE batch_id='journaled-stale'"
        ).fetchone() is not None


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
            match_eligible_postings=2,
            needs_review_postings=0,
            selection_eligible_postings=2,
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
    monkeypatch.setattr(live_runner, "create_repository", lambda _: object())
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
        live_runner, "create_repository", lambda _: SimpleNamespace(remote_url="")
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
        live_runner, "create_repository", lambda _: SimpleNamespace(remote_url="")
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
        live_runner, "create_repository", lambda _: SimpleNamespace(remote_url="")
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
    monkeypatch.setattr(live_runner, "create_repository", lambda _: object())
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
    batch = SimpleNamespace(
        batch_id="batch-1", generation_id="generation-1", selected_count=5
    )
    discarded = []

    def discard_prepared(batch_id, *, expected_generation_id):
        discarded.append((batch_id, expected_generation_id))
        return batch

    store = SimpleNamespace(discard_prepared=discard_prepared)
    monkeypatch.setattr(
        live_runner,
        "_runtime_plan",
        lambda _: (SimpleNamespace(plan_id="pilot"), tmp_path / "brief.json"),
    )
    monkeypatch.setattr(
        live_runner, "load_search_brief", lambda _: SimpleNamespace(client_id="client")
    )
    monkeypatch.setattr(live_runner, "create_repository", lambda _: object())
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
    assert discarded == [("batch-1", "generation-1")]

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
        "create_repository",
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
    monkeypatch.setattr(live_runner, "create_repository", lambda _: repository)
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
    monkeypatch.setattr(live_runner, "create_repository", lambda _: repository)
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




def test_empty_manual_export_discards_batch_and_allows_retry():
    batch = SimpleNamespace(
        batch_id="empty-batch",
        generation_id="generation-2",
        selected_count=0,
        shortfall=5,
        request=SimpleNamespace(requested_quota=5),
    )
    observed = {}
    store = SimpleNamespace(
        discard_prepared=lambda batch_id, *, expected_generation_id: observed.update(
            batch_id=batch_id,
            generation_id=expected_generation_id,
        )
    )

    result = live_runner._discard_empty_manual_batch(store, batch)

    assert result == {
        "action": "manual_export_empty",
        "batch_status": "discarded",
        "batch_id": "empty-batch",
        "selected_count": 0,
        "requested_quota": 5,
        "shortfall": 5,
    }
    assert observed == {
        "batch_id": "empty-batch",
        "generation_id": "generation-2",
    }


def test_manual_export_mode_requires_exclusive_operator_run(monkeypatch):
    monkeypatch.setenv("JOBSIFT_DATABASE", "/tmp/jobsift.sqlite3")
    monkeypatch.setenv("JOBSIFT_CSV", "/tmp/jobsift-export.csv")
    monkeypatch.setenv("JOBSIFT_MANUAL_EXPORT", "true")
    monkeypatch.setenv("JOBSIFT_AUTO_RELEASE", "false")
    monkeypatch.setenv("JOBSIFT_DISCARD_PREPARED", "false")
    monkeypatch.setenv("JOBSIFT_VALIDATION_ONLY", "false")
    monkeypatch.setenv("JOBSIFT_PROFILE_MANAGED", "false")

    config = LiveRunnerConfig.from_env()
    assert config.manual_export is True
    assert config.auto_release is False
    assert config.validation_only is False

    monkeypatch.setenv("JOBSIFT_AUTO_RELEASE", "true")
    with pytest.raises(ValueError, match="manual export cannot be combined"):
        LiveRunnerConfig.from_env()


def test_manual_export_materializes_only_frozen_delivered_rows(tmp_path):
    rows = [
        {
            "Job Title": "Software Engineer",
            "Company Name": "Example",
            "Job Link": "https://example.com/careers/123",
            "Job Description": "Build reliable software",
            "Job Platform": "greenhouse",
        }
    ]
    store = SimpleNamespace(export_rows=lambda batch_id: rows)
    batch = SimpleNamespace(batch_id="frozen-batch", status="delivered")
    first_path = tmp_path / "first" / "jobs.csv"
    second_path = tmp_path / "second" / "jobs.csv"

    live_runner._materialize_manual_export(store, batch, first_path)
    first_bytes = first_path.read_bytes()
    live_runner._materialize_manual_export(store, batch, first_path)
    live_runner._materialize_manual_export(store, batch, second_path)

    assert first_path.read_bytes() == first_bytes
    assert second_path.read_bytes() == first_bytes
    assert first_bytes.count(b"https://example.com/careers/123") == 1


def test_manual_export_replay_uses_csv_destination_without_sheet_lookup(tmp_path, monkeypatch):
    csv_path = tmp_path / "operator-export" / "jobs.csv"
    config = LiveRunnerConfig(
        plan_path=tmp_path / "plan.json",
        database_path=tmp_path / "jobs.sqlite3",
        csv_path=csv_path,
        reports_dir=tmp_path / "runs",
        spreadsheet_id=None,
        sheet_tab=None,
        quota=5,
        interval_seconds=86400,
        timezone="Africa/Lagos",
        auto_release=False,
        allow_partial=False,
        run_once=True,
        manual_export=True,
    )
    batch = SimpleNamespace(batch_id="delivered-batch", status="delivered")
    rows = [
        {
            "Job Title": "Software Engineer",
            "Company Name": "Example",
            "Job Link": "https://example.com/careers/123",
            "Job Description": "Build reliable software",
            "Job Platform": "greenhouse",
        }
    ]
    store = SimpleNamespace(export_rows=lambda batch_id: rows)
    repository = SimpleNamespace(remote_url="")
    observed = {}

    monkeypatch.setattr(
        live_runner,
        "_runtime_plan",
        lambda _: (SimpleNamespace(plan_id="pilot"), tmp_path / "brief.json"),
    )
    monkeypatch.setattr(
        live_runner, "load_search_brief", lambda _: SimpleNamespace(client_id="client")
    )
    monkeypatch.setattr(live_runner, "create_repository", lambda _: repository)
    monkeypatch.setattr(live_runner, "DailyBatchStore", lambda _: store)
    monkeypatch.setattr(
        live_runner,
        "ClientSheetDestinationStore",
        lambda _: (_ for _ in ()).throw(AssertionError("manual export must not inspect Sheets")),
    )
    monkeypatch.setattr(live_runner, "_unresolved_batch", lambda *_, **__: None)

    def existing_batch(repository, *, client_id, destination, idempotency_key):
        observed["destination"] = destination
        return batch

    monkeypatch.setattr(live_runner, "_batch_by_idempotency", existing_batch)
    monkeypatch.setattr(
        live_runner,
        "_batch_payload",
        lambda store, batch, *, action, **kwargs: {"action": action},
    )

    result = live_runner.run_once(config)

    assert result == {"action": "already_exported_today"}
    assert observed["destination"] == str(csv_path.resolve())
    assert csv_path.is_file()
    assert b"https://example.com/careers/123" in csv_path.read_bytes()


def test_manual_export_runner_fails_closed_when_batch_is_not_delivered(monkeypatch):
    config = SimpleNamespace(manual_export=True, run_once=True)
    monkeypatch.setattr(LiveRunnerConfig, "from_env", lambda: config)
    monkeypatch.setattr(
        live_runner,
        "run_once",
        lambda _: {"action": "manual_export_failed", "batch_status": "failed"},
    )

    with pytest.raises(RuntimeError, match="manual CSV export did not reach delivered status"):
        live_runner.main()


def test_manual_export_fresh_run_finalizes_local_csv_without_sheet_access(tmp_path, monkeypatch):
    brief_path = tmp_path / "brief.json"
    brief_path.write_text("{}", encoding="utf-8")
    csv_path = tmp_path / "operator-export" / "jobs.csv"
    config = LiveRunnerConfig(
        plan_path=tmp_path / "plan.json",
        database_path=tmp_path / "jobs.sqlite3",
        csv_path=csv_path,
        reports_dir=tmp_path / "runs",
        spreadsheet_id=None,
        sheet_tab=None,
        quota=5,
        interval_seconds=86400,
        timezone="Africa/Lagos",
        auto_release=False,
        allow_partial=False,
        run_once=True,
        manual_export=True,
    )
    completed_at = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
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
        run_id="inventory-run",
        status="success",
        started_at=completed_at,
        completed_at=completed_at,
        targets=[],
    )
    evaluation = SimpleNamespace(
        run_id=report.run_id,
        evaluated_at=completed_at,
        total_matched=1,
        total_needs_review=0,
        total_rejected=0,
    )
    prepared = SimpleNamespace(
        batch_id="manual-batch",
        generation_id="generation-1",
        status="prepared",
        selected_count=1,
        shortfall=4,
        request=SimpleNamespace(requested_quota=5),
    )
    delivered = SimpleNamespace(
        batch_id="manual-batch",
        generation_id="generation-1",
        status="delivered",
    )
    repository = SimpleNamespace(remote_url="")
    store = SimpleNamespace(
        evidence_digest=lambda *args, **kwargs: "a" * 64,
        export_rows=lambda batch_id: [],
    )
    captured = {}

    monkeypatch.setattr(live_runner, "_runtime_plan", lambda _: (plan, brief_path))
    monkeypatch.setattr(live_runner, "load_search_brief", lambda _: brief)
    monkeypatch.setattr(live_runner, "create_repository", lambda _: repository)
    monkeypatch.setattr(live_runner, "DailyBatchStore", lambda _: store)
    monkeypatch.setattr(
        live_runner,
        "ClientSheetDestinationStore",
        lambda _: (_ for _ in ()).throw(AssertionError("manual export must not inspect Sheets")),
    )
    monkeypatch.setattr(live_runner, "_unresolved_batch", lambda *_, **__: None)
    monkeypatch.setattr(live_runner, "_batch_by_idempotency", lambda *_, **__: None)
    monkeypatch.setattr(live_runner, "collect_inventory_plan", lambda *_, **__: report)
    monkeypatch.setattr(live_runner, "evaluate_inventory_run", lambda **_: evaluation)
    monkeypatch.setattr(
        live_runner,
        "InventoryRunStore",
        lambda _: SimpleNamespace(active_job_ids=lambda *args, **kwargs: ("job-1",)),
    )
    monkeypatch.setattr(live_runner, "_source_failures", lambda _: ())

    def prepare(*, repository, request):
        captured["request"] = request
        return prepared

    def finalize(*, repository, batch_id, expected_generation_id):
        captured["finalize"] = (batch_id, expected_generation_id)
        return delivered

    monkeypatch.setattr(live_runner, "prepare_daily_batch", prepare)
    monkeypatch.setattr(live_runner, "finalize_daily_batch", finalize)
    monkeypatch.setattr(
        live_runner,
        "_batch_payload",
        lambda store, batch, *, action, **kwargs: {
            "action": action,
            "batch_status": batch.status,
        },
    )

    result = live_runner.run_once(config)

    assert result == {"action": "manual_exported", "batch_status": "delivered"}
    assert captured["request"].destination == str(csv_path.resolve())
    assert captured["request"].destination_id is None
    assert captured["finalize"] == ("manual-batch", "generation-1")


def test_live_export_plan_enforces_5_day_us_fully_remote_fail_closed_profile():
    plan_path = (
        Path(__file__).resolve().parents[1]
        / "config/sourcing_plans/taiwo_software_remote_us_v2.json"
    )
    plan = SourcingPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    brief = live_runner.load_search_brief(
        (plan_path.parent / plan.search_brief).resolve()
    )

    assert brief.target_market.countries == {"United States"}
    assert brief.target_market.intent.value == "must"
    assert brief.target_market.unknown_policy.value == "reject"
    assert {mode.value for mode in brief.work_mode.modes} == {"remote"}
    assert brief.work_mode.intent.value == "must"
    assert brief.work_mode.unknown_policy.value == "reject"
    assert brief.posting_freshness.max_age_hours == 72
    assert brief.posting_freshness.unknown_policy.value == "reject"


def test_manual_export_artifact_upload_is_success_only_and_short_lived():
    workflow_path = (
        Path(__file__).resolve().parents[1] / ".github/workflows/live-jobs.yml"
    )
    workflow = workflow_path.read_text(encoding="utf-8")
    artifact_step = workflow.index("Upload operator-owned CSV export")
    artifact_config = workflow[artifact_step:]

    assert "Verify manual export artifacts" in workflow
    assert "test -s /tmp/latest-sourcing.csv" in workflow
    assert "test -s /tmp/jobsift-run-result.json" in workflow
    assert "steps.verify-manual-export.outputs.export_ready == 'true'" in artifact_config
    assert "uses: actions/upload-artifact@v4" in artifact_config
    assert "/tmp/latest-sourcing.csv" in artifact_config
    assert "/tmp/jobsift-run-result.json" in artifact_config
    assert "retention-days: 3" in artifact_config



def test_manual_export_rejects_direct_validation_configuration(tmp_path):
    with pytest.raises(ValueError, match="manual export cannot be combined"):
        LiveRunnerConfig(
            plan_path=tmp_path / "plan.json",
            database_path=tmp_path / "jobs.sqlite3",
            csv_path=tmp_path / "jobs.csv",
            reports_dir=tmp_path / "runs",
            spreadsheet_id=None,
            sheet_tab=None,
            quota=5,
            interval_seconds=86400,
            timezone="Africa/Lagos",
            auto_release=False,
            allow_partial=False,
            run_once=True,
            validation_only=True,
            manual_export=True,
        )


def test_manual_export_ignores_inherited_sheet_settings(monkeypatch):
    monkeypatch.setenv("JOBSIFT_DATABASE", "/tmp/jobsift.sqlite3")
    monkeypatch.setenv("JOBSIFT_CSV", "/tmp/jobsift-export.csv")
    monkeypatch.setenv("JOBSIFT_MANUAL_EXPORT", "true")
    monkeypatch.setenv("JOBSIFT_AUTO_RELEASE", "false")
    monkeypatch.setenv("JOBSIFT_DISCARD_PREPARED", "false")
    monkeypatch.setenv("JOBSIFT_VALIDATION_ONLY", "false")
    monkeypatch.setenv("JOBSIFT_PROFILE_MANAGED", "false")
    monkeypatch.setenv("JOBSIFT_SHEET_ID", "inherited-sheet-id")
    monkeypatch.delenv("JOBSIFT_SHEET_TAB", raising=False)

    config = LiveRunnerConfig.from_env()

    assert config.manual_export is True
    assert config.spreadsheet_id is None
    assert config.sheet_tab is None


def test_run_once_discards_resumed_empty_manual_batch_generation(tmp_path, monkeypatch):
    brief_path = tmp_path / "brief.json"
    brief_path.write_text("{}", encoding="utf-8")
    config = LiveRunnerConfig(
        plan_path=tmp_path / "plan.json",
        database_path=tmp_path / "jobs.sqlite3",
        csv_path=tmp_path / "jobs.csv",
        reports_dir=tmp_path / "runs",
        spreadsheet_id=None,
        sheet_tab=None,
        quota=5,
        interval_seconds=86400,
        timezone="Africa/Lagos",
        auto_release=False,
        allow_partial=False,
        run_once=True,
        manual_export=True,
    )
    empty_batch = SimpleNamespace(
        batch_id="resumed-empty",
        generation_id="generation-9",
        status="prepared",
        selected_count=0,
        shortfall=5,
        request=SimpleNamespace(requested_quota=5),
    )
    observed = {}
    store = SimpleNamespace(
        discard_prepared=lambda batch_id, *, expected_generation_id: observed.update(
            batch_id=batch_id, generation_id=expected_generation_id
        )
    )
    repository = SimpleNamespace(remote_url="")
    plan = SimpleNamespace(plan_id="operator-plan")
    brief = SimpleNamespace(client_id="operator-client")
    monkeypatch.setattr(live_runner, "_runtime_plan", lambda _: (plan, brief_path))
    monkeypatch.setattr(live_runner, "load_search_brief", lambda _: brief)
    monkeypatch.setattr(live_runner, "DailyBatchStore", lambda _: store)
    monkeypatch.setattr(
        live_runner,
        "_unresolved_batch",
        lambda *_, **__: empty_batch,
    )

    result = live_runner.run_once(config, repository=repository)

    assert result["action"] == "manual_export_empty"
    assert result["batch_id"] == "resumed-empty"
    assert observed == {
        "batch_id": "resumed-empty",
        "generation_id": "generation-9",
    }



def test_stale_manual_csv_recovery_discards_only_before_image(tmp_path):
    path = tmp_path / "jobs.csv"
    batch = SimpleNamespace(
        batch_id="stale-manual",
        generation_id="generation-10",
        status="failed",
        error="prepared posting is no longer fresh at delivery: stale",
    )
    observed = {}
    store = SimpleNamespace(
        export_journal=lambda *_, **__: ("absent", "after-image"),
        discard_stale_manual_export=lambda batch_id, **kwargs: observed.update(
            batch_id=batch_id, **kwargs
        ),
    )

    assert live_runner._discard_stale_manual_export_if_unpublished(
        store, batch, path
    )
    assert observed == {
        "batch_id": "stale-manual",
        "expected_generation_id": "generation-10",
        "observed_destination_digest": "absent",
    }


def test_stale_manual_csv_recovery_preserves_changed_destination(tmp_path):
    path = tmp_path / "jobs.csv"
    path.write_text("some output", encoding="utf-8")
    batch = SimpleNamespace(
        batch_id="stale-manual",
        generation_id="generation-11",
        status="failed",
        error="prepared posting is no longer fresh at delivery: stale",
    )
    observed = []
    store = SimpleNamespace(
        export_journal=lambda *_, **__: ("before-image", "after-image"),
        discard_stale_manual_export=lambda *_, **__: observed.append("discarded"),
    )

    assert not live_runner._discard_stale_manual_export_if_unpublished(
        store, batch, path
    )
    assert observed == []

def test_manual_export_empty_outcome_exits_successfully(monkeypatch):
    config = SimpleNamespace(manual_export=True, run_once=True, interval_seconds=1)
    monkeypatch.setattr(LiveRunnerConfig, "from_env", lambda: config)
    monkeypatch.setattr(
        live_runner,
        "run_once",
        lambda _: {"action": "manual_export_empty", "batch_status": "discarded"},
    )

    live_runner.main()


def test_manual_export_requires_delivery_for_nonempty_outcome(monkeypatch):
    config = SimpleNamespace(manual_export=True, run_once=True, interval_seconds=1)
    monkeypatch.setattr(LiveRunnerConfig, "from_env", lambda: config)
    monkeypatch.setattr(
        live_runner,
        "run_once",
        lambda _: {"action": "released", "batch_status": "prepared"},
    )

    with pytest.raises(RuntimeError, match="manual CSV export did not reach delivered status"):
        live_runner.main()
