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
        profile_delivery_runner, "create_repository", lambda _path: object()
    )
    monkeypatch.setattr(
        profile_delivery_runner, "ClientDeliveryProfileStore", lambda _repo: store
    )
    monkeypatch.setattr(
        profile_delivery_runner,
        "OperatorClientStore",
        lambda _repo: SimpleNamespace(get_for_profile=lambda *_args: None),
    )
    monkeypatch.setattr(
        profile_delivery_runner, "GoogleSheetsGateway", lambda: object()
    )
    monkeypatch.setattr(
        profile_delivery_runner,
        "resolve_plan",
        lambda _plan_dir, _plan_id: tmp_path / "plan.json",
    )

    def run_once_after_reconciliation(_config, *, repository=None):
        assert repository is not None
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


def test_profile_runner_main_fails_when_batch_delivery_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBSIFT_DATABASE", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("JOBSIFT_PLAN_DIR", str(tmp_path))
    monkeypatch.setenv("JOBSIFT_REPORTS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(
        profile_delivery_runner,
        "run_active_profiles",
        lambda **_kwargs: [
            {
                "profile_id": "profile-a",
                "action": "resumed_release",
                "batch_status": "failed",
            }
        ],
    )

    try:
        profile_delivery_runner.main()
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("failed delivery batch must fail the operational runner")


def test_profile_runner_uses_durable_operator_brief_without_repo_plan(tmp_path, monkeypatch):
    from job_scout.domain.models import SearchBrief
    from job_scout.operator_clients import brief_sha256

    profile = SimpleNamespace(
        client_id="client-a",
        destination_id="jobs",
        sourcing_plan_id="operator-client-a",
        daily_quota=100,
        status="active",
        delivery_mode="review",
        timezone="Africa/Lagos",
    )
    profile_store = SimpleNamespace(
        active=lambda: (profile,),
        reconcile_destination_sheet=lambda _profile, *, gateway: {
            "observed_links": 0,
            "newly_recorded_links": 0,
        },
    )
    brief = SearchBrief(
        client_id="client-a",
        target_roles=["Software Engineer"],
    )
    managed = SimpleNamespace(
        client_id="client-a",
        destination_id="jobs",
        sourcing_plan_id="operator-client-a",
        brief=brief,
        brief_revision=3,
        brief_sha256=brief_sha256(brief),
    )

    monkeypatch.setattr(
        profile_delivery_runner, "create_repository", lambda _path: object()
    )
    monkeypatch.setattr(
        profile_delivery_runner,
        "ClientDeliveryProfileStore",
        lambda _repo: profile_store,
    )
    monkeypatch.setattr(
        profile_delivery_runner,
        "OperatorClientStore",
        lambda _repo: SimpleNamespace(get_for_profile=lambda *_args: managed),
    )
    monkeypatch.setattr(
        profile_delivery_runner, "GoogleSheetsGateway", lambda: object()
    )
    monkeypatch.setattr(
        profile_delivery_runner,
        "resolve_plan",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("dynamic client must not resolve a repository plan")
        ),
    )

    captured = {}

    def fake_run_once(config, *, repository=None, **kwargs):
        captured.update(kwargs)
        assert config.source_before_delivery is False
        assert repository is not None
        return {"action": "prepared"}

    monkeypatch.setattr(profile_delivery_runner, "run_once", fake_run_once)

    result = profile_delivery_runner.run_active_profiles(
        database_path=tmp_path / "jobs.db",
        plan_dir=tmp_path,
        reports_dir=tmp_path / "runs",
    )

    assert result[0]["action"] == "prepared"
    assert captured["brief_override"] == brief
    assert captured["plan_id_override"] == "operator-client-a"
    assert captured["brief_revision_id_override"] == "operator-r3"
    assert captured["brief_sha256_override"] == brief_sha256(brief)
