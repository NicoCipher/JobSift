"""Strict multi-source sourcing-plan configuration and orchestration."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from job_scout.collectors.ashby import AshbyCollector
from job_scout.collectors.base import JobCollector
from job_scout.collectors.greenhouse import GreenhouseCollector
from job_scout.collectors.lever import LeverCollector
from job_scout.collectors.workday import WorkdayCollector
from job_scout.domain.models import (
    CollectionResult,
    LeverTargetConfig,
    SourceTarget,
    WorkdayTargetConfig,
)
from job_scout.orchestration.pipeline import PipelineSummary, run_pipeline
from job_scout.search_brief import load_search_brief
from job_scout.storage.sqlite import SQLiteRepository


def _non_blank(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("must not be blank")
    return value


def _coordinate(value: str) -> str:
    value = _non_blank(value)
    if any(character.isspace() for character in value):
        raise ValueError("must not contain whitespace")
    return value


class _PlanTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    company: str

    @field_validator("company")
    @classmethod
    def company_required(cls, value: str) -> str:
        return _non_blank(value)

    @property
    def target_identity(self) -> str:
        raise NotImplementedError

    def source_target(self) -> SourceTarget:
        raise NotImplementedError


class GreenhousePlanTarget(_PlanTarget):
    source: Literal["greenhouse"]
    board: str

    @field_validator("board")
    @classmethod
    def board_required(cls, value: str) -> str:
        return _coordinate(value)

    @property
    def target_identity(self) -> str:
        return f"greenhouse:{self.board}"

    def source_target(self) -> SourceTarget:
        return SourceTarget(board_id=self.board, company=self.company)


class AshbyPlanTarget(_PlanTarget):
    source: Literal["ashby"]
    board: str

    @field_validator("board")
    @classmethod
    def board_required(cls, value: str) -> str:
        return _coordinate(value)

    @property
    def target_identity(self) -> str:
        return f"ashby:{self.board}"

    def source_target(self) -> SourceTarget:
        return SourceTarget(board_id=self.board, company=self.company)


class WorkdayPlanTarget(_PlanTarget):
    source: Literal["workday"]
    host: str
    tenant: str
    site: str

    @field_validator("host", "tenant", "site")
    @classmethod
    def coordinates_required(cls, value: str) -> str:
        return _coordinate(value)

    @property
    def config(self) -> WorkdayTargetConfig:
        return WorkdayTargetConfig(host=self.host, tenant=self.tenant, site=self.site)

    @property
    def target_identity(self) -> str:
        return f"workday:{self.config.board_id}"

    def source_target(self) -> SourceTarget:
        config = self.config
        return SourceTarget(board_id=config.board_id, company=self.company, workday=config)


class LeverPlanTarget(_PlanTarget):
    source: Literal["lever"]
    instance: Literal["global", "eu"]
    site: str

    @field_validator("site")
    @classmethod
    def site_required(cls, value: str) -> str:
        return _coordinate(value)

    @property
    def config(self) -> LeverTargetConfig:
        return LeverTargetConfig(instance=self.instance, site=self.site)

    @property
    def target_identity(self) -> str:
        return f"lever:{self.config.board_id}"

    def source_target(self) -> SourceTarget:
        config = self.config
        return SourceTarget(board_id=config.board_id, company=self.company, lever=config)


PlanTarget = Annotated[
    GreenhousePlanTarget | AshbyPlanTarget | WorkdayPlanTarget | LeverPlanTarget,
    Field(discriminator="source"),
]


class SourcingPlan(BaseModel):
    """Where JobSift should look for vacancies for one SearchBrief."""

    model_config = ConfigDict(extra="forbid")

    plan_version: Literal["sourcing-plan-v1"] = "sourcing-plan-v1"
    plan_id: str
    search_brief: str
    database: str
    csv: str
    targets: list[PlanTarget] = Field(min_length=1)

    @field_validator("plan_id")
    @classmethod
    def plan_id_is_path_safe(cls, value: str) -> str:
        value = _non_blank(value)
        if not all(character.isalnum() or character in "._-" for character in value):
            raise ValueError("must contain only letters, numbers, dots, underscores, or hyphens")
        return value

    @field_validator("search_brief", "database", "csv")
    @classmethod
    def path_required(cls, value: str) -> str:
        return _non_blank(value)

    @model_validator(mode="after")
    def distinct_target_identities(self) -> SourcingPlan:
        identities = [target.target_identity for target in self.targets]
        duplicates = sorted({identity for identity in identities if identities.count(identity) > 1})
        if duplicates:
            raise ValueError(f"duplicate target identities: {', '.join(duplicates)}")
        return self


class SourcingTargetReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_identity: str
    source: str
    company: str
    status: str
    received: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    matched: int = 0
    rejected: int = 0
    exported: int = 0
    errors: list[str] = Field(default_factory=list)


class SourcingRunReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str
    started_at: datetime
    completed_at: datetime
    status: Literal["success", "partial", "failure"]
    total_targets: int
    successful_targets: int
    partial_targets: int
    failed_targets: int
    total_received: int
    total_new: int
    total_changed: int
    total_unchanged: int
    total_matched: int
    total_rejected: int
    total_exported: int
    targets: list[SourcingTargetReport]
    report_path: str | None = None


class _ObservedCollector:
    def __init__(self, collector: JobCollector) -> None:
        self.collector = collector
        self.source = collector.source
        self.result: CollectionResult | None = None

    def collect(self, target: SourceTarget) -> CollectionResult:
        self.result = self.collector.collect(target)
        return self.result


CollectorFactory = Callable[[str], JobCollector]


def default_collector_factory(source: str) -> JobCollector:
    return {
        "greenhouse": GreenhouseCollector,
        "ashby": AshbyCollector,
        "workday": WorkdayCollector,
        "lever": LeverCollector,
    }[source]()


def load_sourcing_plan(path: Path) -> SourcingPlan:
    return SourcingPlan.model_validate_json(path.read_text(encoding="utf-8"))


def _resolve_path(value: str, base_dir: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base_dir / path


def _close_collector(collector: JobCollector) -> None:
    client = getattr(collector, "client", None)
    if client is not None:
        client.close()


def _target_report(
    plan_target: PlanTarget,
    summary: PipelineSummary,
    result: CollectionResult | None,
    extra_error: str | None = None,
) -> SourcingTargetReport:
    errors = list(result.errors) if result is not None else []
    if extra_error:
        errors.append(extra_error)
    return SourcingTargetReport(
        target_identity=plan_target.target_identity,
        source=plan_target.source,
        company=plan_target.company,
        status=summary.status,
        received=summary.received,
        new=summary.new,
        changed=summary.changed,
        unchanged=summary.unchanged,
        matched=summary.matched,
        rejected=summary.rejected,
        exported=summary.exported,
        errors=errors,
    )


def run_sourcing_plan(
    plan: SourcingPlan,
    *,
    base_dir: Path | None = None,
    reports_dir: Path | None = None,
    collector_factory: CollectorFactory = default_collector_factory,
) -> SourcingRunReport:
    """Run every configured target sequentially through the existing pipeline."""
    base_dir = (base_dir or Path.cwd()).resolve()
    reports_dir = (reports_dir or Path("runs").resolve()).resolve()
    started_at = datetime.now(UTC)
    brief = load_search_brief(_resolve_path(plan.search_brief, base_dir))
    database_path = _resolve_path(plan.database, base_dir)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    repository = SQLiteRepository(database_path)
    csv_path = _resolve_path(plan.csv, base_dir)
    target_reports: list[SourcingTargetReport] = []

    for plan_target in plan.targets:
        collector: JobCollector | None = None
        observer: _ObservedCollector | None = None
        try:
            collector = collector_factory(plan_target.source)
            observer = _ObservedCollector(collector)
            summary = run_pipeline(
                collector=observer,
                target=plan_target.source_target(),
                profile=brief,
                repository=repository,
                csv_path=csv_path,
            )
            target_reports.append(_target_report(plan_target, summary, observer.result))
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            # Keep operational target errors from stopping later targets.
            target_reports.append(
                _target_report(
                    plan_target,
                    PipelineSummary(status="provider_error"),
                    observer.result if observer else None,
                    f"orchestration error: {type(exc).__name__}: {exc}",
                )
            )
        finally:
            if collector is not None:
                _close_collector(collector)

    successful = sum(report.status == "success" for report in target_reports)
    partial = sum(report.status == "partial" for report in target_reports)
    failed = len(target_reports) - successful - partial
    status: Literal["success", "partial", "failure"]
    status = (
        "success"
        if not partial and not failed
        else "partial"
        if successful or partial
        else "failure"
    )
    totals = {
        name: sum(getattr(report, name) for report in target_reports)
        for name in ("received", "new", "changed", "unchanged", "matched", "rejected", "exported")
    }
    report = SourcingRunReport(
        plan_id=plan.plan_id,
        started_at=started_at,
        completed_at=datetime.now(UTC),
        status=status,
        total_targets=len(target_reports),
        successful_targets=successful,
        partial_targets=partial,
        failed_targets=failed,
        total_received=totals["received"],
        total_new=totals["new"],
        total_changed=totals["changed"],
        total_unchanged=totals["unchanged"],
        total_matched=totals["matched"],
        total_rejected=totals["rejected"],
        total_exported=totals["exported"],
        targets=target_reports,
    )
    output = reports_dir / plan.plan_id
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / f"{report.started_at.strftime('%Y%m%dT%H%M%S%fZ')}.json"
    report.report_path = str(report_path)
    report_path.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report
