from __future__ import annotations

import csv
import json
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from job_scout import cli
from job_scout.domain.models import (
    CollectionResult,
    CollectionStatus,
    Job,
    RemoteStatus,
    RuleIntent,
    SearchBrief,
    SourceTarget,
)
from job_scout.sourcing_plan import SourcingPlan, SourcingRunReport, run_sourcing_plan


def targets() -> list[dict[str, str]]:
    return [
        {"source": "greenhouse", "company": "Green Co", "board": "green"},
        {"source": "ashby", "company": "Ash Co", "board": "ash"},
        {
            "source": "workday",
            "company": "Work Co",
            "host": "work.wd1.myworkdayjobs.com",
            "tenant": "work",
            "site": "External",
        },
        {"source": "lever", "company": "Lever Co", "instance": "eu", "site": "lever"},
    ]


def plan(tmp_path: Path, target_items: list[dict[str, str]] | None = None) -> SourcingPlan:
    brief = SearchBrief(
        client_id="plan-client",
        target_roles=["Support Engineer"],
        management_roles=RuleIntent.IGNORE,
    )
    (tmp_path / "brief.json").write_text(brief.model_dump_json(), encoding="utf-8")
    return SourcingPlan.model_validate(
        {
            "plan_version": "sourcing-plan-v1",
            "plan_id": "mixed-plan",
            "search_brief": "brief.json",
            "database": "state/jobs.sqlite3",
            "csv": "exports/jobs.csv",
            "targets": target_items or targets(),
        }
    )


def fixture_job(source: str, target: SourceTarget) -> Job:
    source_id = f"{source}-{target.board_id}"
    url = f"https://example.test/{source}/{target.board_id}/{source_id}"
    description = "Support customers with investigations and product questions. " * 12
    return Job(
        id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source}:{target.board_id}:{source_id}")),
        source=source,
        source_job_id=source_id,
        source_board_id=target.board_id,
        title="Support Engineer",
        company=target.company,
        description_text=description,
        job_url=url,
        canonical_url=url,
        apply_url=f"{url}/apply",
        country="United States",
        eligible_countries={"United States"},
        remote_status=RemoteStatus.REMOTE,
        content_fingerprint=source_id,
    )


class FixtureCollector:
    def __init__(
        self,
        source: str,
        calls: list[str],
        *,
        status: CollectionStatus = CollectionStatus.SUCCESS,
    ) -> None:
        self.source = source
        self.calls = calls
        self.status = status

    def collect(self, target: SourceTarget) -> CollectionResult:
        self.calls.append(self.source)
        if self.status is not CollectionStatus.SUCCESS:
            return CollectionResult(
                source=self.source,
                target=target,
                status=self.status,
                errors=[f"{self.source} fixture failure"],
            )
        return CollectionResult(
            source=self.source,
            target=target,
            status=self.status,
            jobs=[fixture_job(self.source, target)],
        )


def factory(calls: list[str], failures: set[str] | None = None):
    failures = failures or set()

    def build(source: str) -> FixtureCollector:
        return FixtureCollector(
            source,
            calls,
            status=CollectionStatus.PROVIDER_ERROR
            if source in failures
            else CollectionStatus.SUCCESS,
        )

    return build


def test_valid_mixed_plan_exposes_explicit_target_identities(tmp_path: Path) -> None:
    sourcing_plan = plan(tmp_path)

    assert [target.target_identity for target in sourcing_plan.targets] == [
        "greenhouse:green",
        "ashby:ash",
        "workday:work.wd1.myworkdayjobs.com:work:External",
        "lever:eu:lever",
    ]
    workday = sourcing_plan.targets[2].source_target()
    lever = sourcing_plan.targets[3].source_target()
    assert workday.workday and workday.board_id == workday.workday.board_id
    assert lever.lever and lever.board_id == lever.lever.board_id


@pytest.mark.parametrize(
    "target",
    [
        {"source": "lever", "company": "Lever", "instance": "us", "site": "board"},
        {"source": "workday", "company": "Work", "host": "host", "tenant": "", "site": "x"},
        {"source": "greenhouse", "company": "Green", "board": "has space"},
        {"source": "ashby", "company": "Ash", "board": "board", "unknown": "no"},
    ],
)
def test_malformed_source_specific_targets_are_rejected(
    tmp_path: Path, target: dict[str, str]
) -> None:
    with pytest.raises(ValidationError):
        plan(tmp_path, [target])


def test_duplicate_identity_is_rejected_before_collector_creation(tmp_path: Path) -> None:
    duplicate = [
        {"source": "greenhouse", "company": "One", "board": "same"},
        {"source": "greenhouse", "company": "Two", "board": "same"},
    ]
    calls: list[str] = []

    with pytest.raises(ValidationError, match="duplicate target identities"):
        plan(tmp_path, duplicate)

    assert calls == []


def test_targets_run_in_declared_order_with_shared_state_and_report(tmp_path: Path) -> None:
    sourcing_plan = plan(tmp_path)
    calls: list[str] = []
    report = run_sourcing_plan(
        sourcing_plan,
        base_dir=tmp_path,
        reports_dir=tmp_path / "runs",
        collector_factory=factory(calls),
    )

    assert calls == ["greenhouse", "ashby", "workday", "lever"]
    assert report.status == "success"
    assert report.total_targets == report.successful_targets == 4
    assert report.total_received == report.total_new == report.total_exported == 4
    assert report.total_matched == 4 and report.total_rejected == 0
    assert [item.target_identity for item in report.targets] == [
        target.target_identity for target in sourcing_plan.targets
    ]
    assert report.report_path and Path(report.report_path).is_file()
    saved = json.loads(Path(report.report_path).read_text())
    assert len(saved["targets"]) == 4
    assert Path(tmp_path / "state/jobs.sqlite3").is_file()
    with (tmp_path / "exports/jobs.csv").open(newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 4
    with sqlite3.connect(tmp_path / "state/jobs.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM job_matches WHERE client_id='plan-client'"
            ).fetchone()[0]
            == 4
        )


def test_failure_is_isolated_and_reported_without_closure_claim(tmp_path: Path) -> None:
    sourcing_plan = plan(tmp_path)
    calls: list[str] = []
    report = run_sourcing_plan(
        sourcing_plan,
        base_dir=tmp_path,
        reports_dir=tmp_path / "runs",
        collector_factory=factory(calls, {"greenhouse"}),
    )

    assert calls == ["greenhouse", "ashby", "workday", "lever"]
    assert report.status == "partial"
    assert report.failed_targets == 1 and report.successful_targets == 3
    failed = report.targets[0]
    assert failed.status == "provider_error" and failed.received == 0
    assert failed.errors == ["greenhouse fixture failure"]
    assert not hasattr(failed, "closed")
    assert report.total_received == sum(item.received for item in report.targets)


@pytest.mark.parametrize("failed_source", ["greenhouse", "ashby", "workday"])
def test_each_earlier_source_failure_still_reaches_later_targets(
    tmp_path: Path, failed_source: str
) -> None:
    calls: list[str] = []
    report = run_sourcing_plan(
        plan(tmp_path),
        base_dir=tmp_path,
        reports_dir=tmp_path / "runs",
        collector_factory=factory(calls, {failed_source}),
    )

    assert calls == ["greenhouse", "ashby", "workday", "lever"]
    assert report.failed_targets == 1 and report.successful_targets == 3
    assert report.total_received == report.total_new == report.total_exported == 3


def test_second_identical_plan_run_is_unchanged_and_suppresses_deliveries(tmp_path: Path) -> None:
    sourcing_plan = plan(tmp_path)
    first = run_sourcing_plan(
        sourcing_plan,
        base_dir=tmp_path,
        reports_dir=tmp_path / "runs",
        collector_factory=factory([]),
    )
    second = run_sourcing_plan(
        sourcing_plan,
        base_dir=tmp_path,
        reports_dir=tmp_path / "runs",
        collector_factory=factory([]),
    )

    assert first.total_new == first.total_exported == 4
    assert second.total_unchanged == 4
    assert second.total_new == second.total_exported == 0


def test_invalid_plan_means_zero_collector_calls(tmp_path: Path) -> None:
    calls: list[str] = []
    payload = {
        "plan_version": "sourcing-plan-v1",
        "plan_id": "bad plan",
        "search_brief": "brief.json",
        "database": "jobs.sqlite3",
        "csv": "jobs.csv",
        "targets": targets(),
    }

    with pytest.raises(ValidationError):
        SourcingPlan.model_validate(payload)

    assert calls == []


def test_source_command_routes_plan_without_changing_collect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = SourcingRunReport(
        plan_id="test",
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:00:01Z",
        status="success",
        total_targets=0,
        successful_targets=0,
        partial_targets=0,
        failed_targets=0,
        total_received=0,
        total_new=0,
        total_changed=0,
        total_unchanged=0,
        total_matched=0,
        total_rejected=0,
        total_exported=0,
        targets=[],
    )
    seen: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_sourcing_plan", lambda path: seen.setdefault("plan", object()))
    monkeypatch.setattr(
        cli,
        "run_sourcing_plan",
        lambda plan, **kwargs: seen.update(kwargs) or expected,
    )
    monkeypatch.setattr(sys, "argv", ["job-scout", "source", "--plan", "example.json"])

    cli.main()

    assert "plan" in seen and Path(seen["base_dir"]).name
    assert Path(seen["reports_dir"]).name == "runs"
