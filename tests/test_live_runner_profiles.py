from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from job_scout import live_runner
from job_scout.live_runner import LiveRunnerConfig


def config(tmp_path: Path, *, profile_managed: bool = True) -> LiveRunnerConfig:
    return LiveRunnerConfig(
        plan_path=tmp_path / "plan.json",
        database_path=tmp_path / "jobs.db",
        csv_path=tmp_path / "jobs.csv",
        reports_dir=tmp_path / "runs",
        spreadsheet_id=None,
        sheet_tab=None,
        destination_id="client-jobs",
        quota=5,
        interval_seconds=86400,
        timezone="Africa/Lagos",
        auto_release=False,
        allow_partial=False,
        run_once=True,
        profile_managed=profile_managed,
    )


def base(monkeypatch, tmp_path, profile, *, delivered=0):
    brief_path = tmp_path / "brief.json"
    brief_path.write_text("{}", encoding="utf-8")
    plan = SimpleNamespace(plan_id="software-us-v1")
    brief = SimpleNamespace(client_id="client-a")
    destination = SimpleNamespace(
        destination_id="client-jobs",
        logical_uri="client-sheet://client-jobs",
        config_sha256="a" * 64,
        status="ready",
    )
    repository = SimpleNamespace()

    monkeypatch.setattr(live_runner, "_runtime_plan", lambda _: (plan, brief_path))
    monkeypatch.setattr(live_runner, "load_search_brief", lambda _: brief)
    monkeypatch.setattr(live_runner, "SQLiteRepository", lambda _: repository)
    monkeypatch.setattr(live_runner, "DailyBatchStore", lambda _: SimpleNamespace())
    monkeypatch.setattr(
        live_runner,
        "ClientSheetDestinationStore",
        lambda _: SimpleNamespace(get=lambda *_args, **_kwargs: destination),
    )
    monkeypatch.setattr(
        live_runner,
        "ClientDeliveryProfileStore",
        lambda _: SimpleNamespace(
            get=lambda *_args: profile,
            delivered_today=lambda *_args, **_kwargs: delivered,
        ),
    )
    return repository, plan, brief, destination


def test_profile_managed_runner_stops_cleanly_when_paused(tmp_path, monkeypatch):
    profile = SimpleNamespace(
        client_id="client-a",
        destination_id="client-jobs",
        sourcing_plan_id="software-us-v1",
        daily_quota=100,
        status="paused",
        delivery_mode="review",
        timezone="Africa/Lagos",
    )
    base(monkeypatch, tmp_path, profile)

    result = live_runner.run_once(config(tmp_path))

    assert result == {
        "action": "profile_paused",
        "client_id": "client-a",
        "destination_id": "client-jobs",
        "daily_quota": 100,
        "delivery_mode": "review",
    }


def test_profile_managed_runner_stops_when_daily_quota_is_reached(tmp_path, monkeypatch):
    profile = SimpleNamespace(
        client_id="client-a",
        destination_id="client-jobs",
        sourcing_plan_id="software-us-v1",
        daily_quota=100,
        status="active",
        delivery_mode="auto",
        timezone="Africa/Lagos",
    )
    base(monkeypatch, tmp_path, profile, delivered=100)

    result = live_runner.run_once(config(tmp_path))

    assert result["action"] == "quota_reached"
    assert result["delivered_today"] == 100
    assert result["remaining_today"] == 0


def test_profile_quota_is_remaining_today_not_full_daily_target(tmp_path, monkeypatch):
    profile = SimpleNamespace(
        client_id="client-a",
        destination_id="client-jobs",
        sourcing_plan_id="software-us-v1",
        daily_quota=100,
        status="active",
        delivery_mode="review",
        timezone="Africa/Lagos",
    )
    repository, _, _, destination = base(monkeypatch, tmp_path, profile, delivered=14)
    repository.prune_stale_inventory = lambda **_: {"deleted_jobs": 0}

    brief = SimpleNamespace(
        client_id="client-a",
        delivery_policy=SimpleNamespace(
            max_jobs_per_employer_per_batch=1,
            employer_cooldown_days=0,
        ),
        posting_freshness=SimpleNamespace(max_age_hours=24, unknown_policy="reject"),
    )
    monkeypatch.setattr(live_runner, "load_search_brief", lambda _: brief)
    monkeypatch.setattr(live_runner, "_unresolved_batch", lambda *_, **__: None)
    monkeypatch.setattr(live_runner, "_batch_by_idempotency", lambda *_, **__: None)
    report = SimpleNamespace(
        run_id="run-1",
        status="success",
        completed_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        targets=[],
        total_received=1,
        successful_targets=1,
        partial_targets=0,
        failed_targets=0,
    )
    monkeypatch.setattr(live_runner, "collect_inventory_plan", lambda *_, **__: report)
    evaluation = SimpleNamespace(
        evaluated_at=report.completed_at,
        total_matched=1,
        total_rejected=0,
    )
    monkeypatch.setattr(live_runner, "evaluate_inventory_run", lambda **_: evaluation)
    monkeypatch.setattr(
        live_runner,
        "InventoryRunStore",
        lambda _: SimpleNamespace(active_job_ids=lambda _run: ("job-1",)),
    )
    store = SimpleNamespace(evidence_digest=lambda *_: "b" * 64)
    monkeypatch.setattr(live_runner, "DailyBatchStore", lambda _: store)
    captured = {}

    def prepare(*, repository, request):
        captured["request"] = request
        return SimpleNamespace(batch_id="batch-1")

    monkeypatch.setattr(live_runner, "prepare_daily_batch", prepare)
    monkeypatch.setattr(
        live_runner,
        "_batch_payload",
        lambda *_args, **kwargs: {
            "action": kwargs["action"],
            "requested_quota": captured["request"].requested_quota,
        },
    )
    monkeypatch.setattr(
        live_runner,
        "ClientDeliveryProfileStore",
        lambda _: SimpleNamespace(
            get=lambda *_args: profile,
            delivered_today=lambda *_args, **_kwargs: 14,
        ),
    )

    result = live_runner.run_once(config(tmp_path))

    assert captured["request"].destination == destination.logical_uri
    assert captured["request"].requested_quota == 86
    assert result["requested_quota"] == 86
