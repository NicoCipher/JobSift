from types import SimpleNamespace

from job_scout import profile_delivery_runner


def test_profile_runner_reconciles_sheet_before_quota_run(tmp_path, monkeypatch):
    profile = SimpleNamespace(
        client_id="client-a",
        destination_id="client-jobs",
        sourcing_plan_id="software-us-v1",
        daily_quota=100,
        status="active",
        delivery_mode="review",
        timezone="Africa/Lagos",
    )
    events = []
    store = SimpleNamespace(
        active=lambda: (profile,),
        reconcile_destination_sheet=lambda _profile, *, gateway: (
            events.append("reconcile")
            or {"observed_links": 14, "newly_recorded_links": 0}
        ),
    )

    monkeypatch.setattr(
        profile_delivery_runner, "SQLiteRepository", lambda _path: object()
    )
    monkeypatch.setattr(
        profile_delivery_runner, "ClientDeliveryProfileStore", lambda _repo: store
    )
    monkeypatch.setattr(
        profile_delivery_runner, "GoogleSheetsGateway", lambda: object()
    )
    monkeypatch.setattr(
        profile_delivery_runner,
        "resolve_plan",
        lambda _plan_dir, _plan_id: tmp_path / "plan.json",
    )

    def run_once_after_reconciliation(_config):
        assert events == ["reconcile"]
        events.append("run")
        return {"action": "quota_reached"}

    monkeypatch.setattr(
        profile_delivery_runner, "run_once", run_once_after_reconciliation
    )

    result = profile_delivery_runner.run_active_profiles(
        database_path=tmp_path / "jobs.db",
        plan_dir=tmp_path,
        reports_dir=tmp_path / "runs",
    )

    assert events == ["reconcile", "run"]
    assert result[0]["action"] == "quota_reached"
