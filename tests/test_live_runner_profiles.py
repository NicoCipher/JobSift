from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from job_scout import live_runner
from job_scout.domain.daily_batch import BatchConflict
from job_scout.live_runner import LiveRunnerConfig


def config(
    tmp_path: Path, *, profile_managed: bool = True, auto_release: bool = False
) -> LiveRunnerConfig:
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
        auto_release=auto_release,
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
    monkeypatch.setattr(live_runner, "create_repository", lambda _: repository)
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
    monkeypatch.setattr(live_runner, "_unresolved_batch", lambda *_, **__: None)
    return repository, plan, brief, destination


def test_legacy_release_uses_existing_profile_pause_guard(tmp_path, monkeypatch):
    profile = SimpleNamespace(
        client_id="client-a",
        destination_id="client-jobs",
        sourcing_plan_id="software-us-v1",
        daily_quota=100,
        status="paused",
        delivery_mode="review",
        timezone="Africa/Lagos",
    )
    base(monkeypatch, tmp_path, profile, delivered=0)
    unresolved = SimpleNamespace(
        batch_id="legacy-batch",
        selected_count=1,
        request=SimpleNamespace(
            client_id="client-a",
            destination_id="client-jobs",
        ),
    )
    monkeypatch.setattr(
        live_runner, "_unresolved_batch", lambda *_, **__: unresolved
    )
    monkeypatch.setattr(live_runner, "GoogleSheetsGateway", lambda: object())

    def blocked_guard(*_args, **_kwargs):
        raise BatchConflict("delivery profile is paused")

    monkeypatch.setattr(
        live_runner,
        "ClientDeliveryProfileStore",
        lambda _: SimpleNamespace(
            get=lambda *_args: profile,
            delivered_today=lambda *_args, **_kwargs: 0,
            guard_batch_release=blocked_guard,
        ),
    )
    monkeypatch.setattr(
        live_runner,
        "finalize_daily_batch",
        lambda **_: (_ for _ in ()).throw(
            AssertionError("legacy release must not bypass the profile guard")
        ),
    )

    result = live_runner.run_once(
        config(tmp_path, profile_managed=False, auto_release=True)
    )

    assert result["action"] == "profile_paused"


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
        lambda _: SimpleNamespace(active_job_ids=lambda _run, **kwargs: ("job-1",)),
    )
    store = SimpleNamespace(evidence_digest=lambda *_, **__: "b" * 64)
    monkeypatch.setattr(live_runner, "DailyBatchStore", lambda _: store)
    captured = {}

    def prepare(*, repository, request):
        captured["request"] = request
        return SimpleNamespace(batch_id="batch-1", selected_count=1)

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


def test_auto_release_does_not_exceed_current_remaining_quota(tmp_path, monkeypatch):
    profile = SimpleNamespace(
        client_id="client-a",
        destination_id="client-jobs",
        sourcing_plan_id="software-us-v1",
        daily_quota=100,
        status="active",
        delivery_mode="auto",
        timezone="Africa/Lagos",
    )
    base(monkeypatch, tmp_path, profile, delivered=90)
    unresolved = SimpleNamespace(batch_id="batch-1", selected_count=20)
    monkeypatch.setattr(
        live_runner, "_unresolved_batch", lambda *_, **__: unresolved
    )
    monkeypatch.setattr(live_runner, "GoogleSheetsGateway", lambda: object())

    def blocked_guard(*_args, **_kwargs):
        raise BatchConflict(
            "prepared batch exceeds the profile's current remaining daily quota"
        )

    monkeypatch.setattr(
        live_runner,
        "ClientDeliveryProfileStore",
        lambda _: SimpleNamespace(
            get=lambda *_args: profile,
            delivered_today=lambda *_args, **_kwargs: 90,
            guard_batch_release=blocked_guard,
        ),
    )
    monkeypatch.setattr(
        live_runner,
        "finalize_daily_batch",
        lambda **_: (_ for _ in ()).throw(
            AssertionError("over-quota unresolved batch must not release")
        ),
    )
    monkeypatch.setattr(
        live_runner,
        "_batch_payload",
        lambda _store, batch, *, action, **_kwargs: {
            "action": action,
            "selected_count": batch.selected_count,
        },
    )

    result = live_runner.run_once(config(tmp_path))

    assert result == {
        "action": "release_blocked_quota",
        "selected_count": 20,
    }


def test_paused_profile_recovers_already_applied_unresolved_export(
    tmp_path, monkeypatch
):
    profile = SimpleNamespace(
        client_id="client-a",
        destination_id="client-jobs",
        sourcing_plan_id="software-us-v1",
        daily_quota=100,
        status="paused",
        delivery_mode="review",
        timezone="Africa/Lagos",
    )
    base(monkeypatch, tmp_path, profile, delivered=1)
    unresolved = SimpleNamespace(
        batch_id="batch-1", generation_id="gen-1", selected_count=1
    )
    recovered = SimpleNamespace(
        batch_id="batch-1",
        generation_id="gen-1",
        selected_count=1,
        status="delivered",
    )
    monkeypatch.setattr(
        live_runner, "_unresolved_batch", lambda *_, **__: unresolved
    )
    monkeypatch.setattr(live_runner, "GoogleSheetsGateway", lambda: object())
    calls = []

    def guard(_profile, batch_id, *, gateway):
        calls.append(("guard", batch_id))
        return unresolved, {"observed_links": 1}, 99

    monkeypatch.setattr(
        live_runner,
        "ClientDeliveryProfileStore",
        lambda _: SimpleNamespace(
            get=lambda *_args: profile,
            delivered_today=lambda *_args, **_kwargs: 1,
            guard_batch_release=guard,
        ),
    )
    monkeypatch.setattr(
        live_runner,
        "finalize_daily_batch",
        lambda **kwargs: calls.append(("finalize", kwargs["batch_id"])) or recovered,
    )
    monkeypatch.setattr(
        live_runner,
        "_batch_payload",
        lambda _store, batch, *, action, **_kwargs: {
            "action": action,
            "batch_status": batch.status,
        },
    )

    result = live_runner.run_once(config(tmp_path))

    assert result == {"action": "recovered_release", "batch_status": "delivered"}
    assert calls == [("guard", "batch-1"), ("finalize", "batch-1")]


def test_profile_runner_recovers_unresolved_export_before_quota_reached(
    tmp_path, monkeypatch
):
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
    unresolved = SimpleNamespace(
        batch_id="batch-1", generation_id="gen-1", selected_count=1
    )
    recovered = SimpleNamespace(
        batch_id="batch-1",
        generation_id="gen-1",
        selected_count=1,
        status="delivered",
    )
    monkeypatch.setattr(
        live_runner, "_unresolved_batch", lambda *_, **__: unresolved
    )
    monkeypatch.setattr(live_runner, "GoogleSheetsGateway", lambda: object())
    calls = []

    def guard(_profile, batch_id, *, gateway):
        calls.append(("guard", batch_id))
        return unresolved, {"observed_links": 1}, 0

    monkeypatch.setattr(
        live_runner,
        "ClientDeliveryProfileStore",
        lambda _: SimpleNamespace(
            get=lambda *_args: profile,
            delivered_today=lambda *_args, **_kwargs: 100,
            guard_batch_release=guard,
        ),
    )
    monkeypatch.setattr(
        live_runner,
        "finalize_daily_batch",
        lambda **kwargs: calls.append(("finalize", kwargs["batch_id"])) or recovered,
    )
    monkeypatch.setattr(
        live_runner,
        "_batch_payload",
        lambda _store, batch, *, action, **_kwargs: {
            "action": action,
            "batch_status": batch.status,
        },
    )

    result = live_runner.run_once(config(tmp_path))

    assert result == {"action": "recovered_release", "batch_status": "delivered"}
    assert calls == [("guard", "batch-1"), ("finalize", "batch-1")]


def test_profile_idempotency_changes_with_inventory_evaluation_scope(
    tmp_path, monkeypatch
):
    profile = SimpleNamespace(
        client_id="client-a",
        destination_id="client-jobs",
        sourcing_plan_id="software-us-v1",
        daily_quota=100,
        status="active",
        delivery_mode="review",
        timezone="Africa/Lagos",
    )
    repository, _, _, _ = base(monkeypatch, tmp_path, profile, delivered=0)
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
    seen_keys = []
    monkeypatch.setattr(
        live_runner,
        "_batch_by_idempotency",
        lambda _repo, *, idempotency_key, **_kwargs: (
            seen_keys.append(idempotency_key) or None
        ),
    )
    report_id = {"value": "run-a"}

    def collect(*_args, **_kwargs):
        return SimpleNamespace(
            run_id=report_id["value"],
            status="success",
            completed_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
            targets=[],
            total_received=0,
            successful_targets=1,
            partial_targets=0,
            failed_targets=0,
        )

    monkeypatch.setattr(live_runner, "collect_inventory_plan", collect)
    monkeypatch.setattr(
        live_runner,
        "evaluate_inventory_run",
        lambda **kwargs: SimpleNamespace(
            evaluated_at=kwargs["evaluated_at"],
            total_matched=0,
            total_rejected=0,
        ),
    )
    monkeypatch.setattr(
        live_runner,
        "InventoryRunStore",
        lambda _: SimpleNamespace(active_job_ids=lambda _run, **kwargs: ()),
    )
    store = SimpleNamespace(
        evidence_digest=lambda *_, **__: "b" * 64,
        discard_prepared=lambda _batch_id, *, expected_generation_id: None,
    )
    monkeypatch.setattr(live_runner, "DailyBatchStore", lambda _: store)
    captured = []

    def prepare(*, repository, request):
        captured.append(request.idempotency_key)
        return SimpleNamespace(
            batch_id=f"batch-{len(captured)}",
            generation_id=f"generation-{len(captured)}",
            selected_count=0,
        )

    monkeypatch.setattr(live_runner, "prepare_daily_batch", prepare)
    monkeypatch.setattr(
        live_runner,
        "_batch_payload",
        lambda *_args, **kwargs: {"action": kwargs["action"]},
    )
    monkeypatch.setattr(
        live_runner,
        "ClientDeliveryProfileStore",
        lambda _: SimpleNamespace(
            get=lambda *_args: profile,
            delivered_today=lambda *_args, **_kwargs: 0,
        ),
    )

    live_runner.run_once(config(tmp_path))
    report_id["value"] = "run-b"
    live_runner.run_once(config(tmp_path))

    assert len(captured) == 2
    assert captured[0] != captured[1]
    assert "delivered-0" not in captured[0]
    assert "delivered-0" not in captured[1]

def test_review_profile_discards_empty_scope_so_next_refresh_can_run(
    tmp_path, monkeypatch
):
    profile = SimpleNamespace(
        client_id="client-a",
        destination_id="client-jobs",
        sourcing_plan_id="software-us-v1",
        daily_quota=100,
        status="active",
        delivery_mode="review",
        timezone="Africa/Lagos",
    )
    repository, _, _, _ = base(monkeypatch, tmp_path, profile, delivered=0)
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
    monkeypatch.setattr(live_runner, "_batch_by_idempotency", lambda *_, **__: None)
    report = SimpleNamespace(
        run_id="empty-run",
        status="success",
        completed_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        targets=[],
        total_received=0,
        successful_targets=1,
        partial_targets=0,
        failed_targets=0,
    )
    monkeypatch.setattr(live_runner, "collect_inventory_plan", lambda *_, **__: report)
    monkeypatch.setattr(
        live_runner,
        "evaluate_inventory_run",
        lambda **kwargs: SimpleNamespace(
            evaluated_at=kwargs["evaluated_at"],
            total_matched=0,
            total_rejected=0,
        ),
    )
    monkeypatch.setattr(
        live_runner,
        "InventoryRunStore",
        lambda _: SimpleNamespace(active_job_ids=lambda _run, **kwargs: ()),
    )

    discarded = []
    store = SimpleNamespace(
        evidence_digest=lambda *_, **__: "b" * 64,
        discard_prepared=lambda batch_id, *, expected_generation_id: discarded.append(
            (batch_id, expected_generation_id)
        ),
    )
    monkeypatch.setattr(live_runner, "DailyBatchStore", lambda _: store)
    monkeypatch.setattr(
        live_runner,
        "prepare_daily_batch",
        lambda **_: SimpleNamespace(
            batch_id="empty-batch",
            generation_id="empty-generation",
            selected_count=0,
        ),
    )
    monkeypatch.setattr(
        live_runner,
        "_batch_payload",
        lambda _store, batch, *, action, **_kwargs: {
            "action": action,
            "batch_id": batch.batch_id,
            "batch_status": "prepared",
            "selected_count": batch.selected_count,
        },
    )

    result = live_runner.run_once(config(tmp_path))

    assert result["action"] == "empty_review_scope"
    assert result["batch_status"] == "discarded"
    assert result["selected_count"] == 0
    assert discarded == [("empty-batch", "empty-generation")]

