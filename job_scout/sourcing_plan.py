"""Strict multi-source sourcing-plan configuration and orchestration."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from job_scout.collectors.ashby import AshbyCollector
from job_scout.collectors.base import JobCollector
from job_scout.collectors.greenhouse import GreenhouseCollector
from job_scout.collectors.lever import LeverCollector
from job_scout.collectors.smartrecruiters import SmartRecruitersCollector
from job_scout.collectors.workday import WorkdayCollector
from job_scout.domain.models import (
    CollectionResult,
    CollectionStatus,
    Job,
    JobLifecycle,
    LeverTargetConfig,
    MatchDecision,
    SearchBrief,
    SourceTarget,
    WorkdayTargetConfig,
)
from job_scout.matching.matcher import match_job
from job_scout.orchestration.pipeline import PipelineSummary, run_pipeline
from job_scout.retention import retention_basis
from job_scout.search_brief import load_search_brief
from job_scout.storage.factory import create_repository
from job_scout.storage.inventory_runs import InventoryRunStore

# Backward-compatible module attribute for older callers/tests; runtime uses create_repository.
SQLiteRepository = create_repository


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
    employer_id: str | None = None

    @field_validator("company")
    @classmethod
    def company_required(cls, value: str) -> str:
        return _non_blank(value)

    @field_validator("employer_id")
    @classmethod
    def employer_id_is_stable(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().casefold()
        if not value or not all(character.isalnum() or character in "._-" for character in value):
            raise ValueError(
                "employer_id must contain only letters, numbers, dots, underscores, or hyphens"
            )
        return value

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
        return SourceTarget(
            board_id=self.board, company=self.company, employer_id=self.employer_id
        )


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
        return SourceTarget(
            board_id=self.board, company=self.company, employer_id=self.employer_id
        )


class SmartRecruitersPlanTarget(_PlanTarget):
    source: Literal["smartrecruiters"]
    board: str

    @field_validator("board")
    @classmethod
    def board_required(cls, value: str) -> str:
        value = _coordinate(value)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value):
            raise ValueError("invalid SmartRecruiters company identifier")
        return value.casefold()

    @property
    def target_identity(self) -> str:
        return f"smartrecruiters:{self.board}"

    def source_target(self) -> SourceTarget:
        return SourceTarget(
            board_id=self.board.casefold(), company=self.company, employer_id=self.employer_id
        )


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
        return SourceTarget(
            board_id=config.board_id,
            company=self.company,
            employer_id=self.employer_id,
            workday=config,
        )


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
        return SourceTarget(
            board_id=config.board_id,
            company=self.company,
            employer_id=self.employer_id,
            lever=config,
        )


PlanTarget = Annotated[
    GreenhousePlanTarget
    | AshbyPlanTarget
    | SmartRecruitersPlanTarget
    | WorkdayPlanTarget
    | LeverPlanTarget,
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


class InventoryTargetReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_identity: str
    source: str
    company: str
    status: str
    received: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    errors: list[str] = Field(default_factory=list)


class InventoryRunReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
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
    targets: list[InventoryTargetReport]
    report_path: str | None = None


class InventoryEvaluationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    client_id: str
    evaluated_at: datetime
    total_evaluated: int
    total_matched: int
    total_needs_review: int = 0
    total_rejected: int


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
        "smartrecruiters": SmartRecruitersCollector,
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


def _inventory_target_report(
    plan_target: PlanTarget,
    *,
    status: str,
    received: int = 0,
    states: dict[str, JobLifecycle] | None = None,
    errors: list[str] | None = None,
) -> InventoryTargetReport:
    states = states or {}
    return InventoryTargetReport(
        target_identity=plan_target.target_identity,
        source=plan_target.source,
        company=plan_target.company,
        status=status,
        received=received,
        new=sum(state is JobLifecycle.NEW for state in states.values()),
        changed=sum(state is JobLifecycle.CHANGED for state in states.values()),
        unchanged=sum(state is JobLifecycle.SEEN for state in states.values()),
        errors=list(errors or []),
    )


def collect_inventory_plan(
    plan: SourcingPlan,
    *,
    repository: SQLiteRepository,
    reports_dir: Path | None = None,
    collector_factory: CollectorFactory = default_collector_factory,
) -> InventoryRunReport:
    """Collect shared source inventory without matching or delivery side effects."""
    reports_dir = (reports_dir or Path("runs").resolve()).resolve()
    started_at = datetime.now(UTC)
    run_id = str(uuid5(NAMESPACE_URL, f"inventory:{plan.plan_id}:{started_at.isoformat()}"))
    inventory = InventoryRunStore(repository)
    inventory.create(run_id=run_id, plan_id=plan.plan_id, started_at=started_at)
    target_reports: list[InventoryTargetReport] = []

    for plan_target in plan.targets:
        collector: JobCollector | None = None
        try:
            collector = collector_factory(plan_target.source)
            result = collector.collect(plan_target.source_target())
            states: dict[str, JobLifecycle] = {}
            received = 0
            if result.status in {CollectionStatus.SUCCESS, CollectionStatus.PARTIAL}:
                received = len(result.jobs)
                states = repository.upsert_jobs(result.jobs)
                inventory.add_jobs(
                    run_id=run_id,
                    target_identity=plan_target.target_identity,
                    jobs=result.jobs,
                )
            target_reports.append(
                _inventory_target_report(
                    plan_target,
                    status=result.status.value,
                    received=received,
                    states=states,
                    errors=result.errors,
                )
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            target_reports.append(
                _inventory_target_report(
                    plan_target,
                    status=CollectionStatus.PROVIDER_ERROR.value,
                    errors=[f"orchestration error: {type(exc).__name__}: {exc}"],
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
    completed_at = datetime.now(UTC)
    inventory.finish(run_id=run_id, status=status, completed_at=completed_at)
    report = InventoryRunReport(
        run_id=run_id,
        plan_id=plan.plan_id,
        started_at=started_at,
        completed_at=completed_at,
        status=status,
        total_targets=len(target_reports),
        successful_targets=successful,
        partial_targets=partial,
        failed_targets=failed,
        total_received=sum(item.received for item in target_reports),
        total_new=sum(item.new for item in target_reports),
        total_changed=sum(item.changed for item in target_reports),
        total_unchanged=sum(item.unchanged for item in target_reports),
        targets=target_reports,
    )
    output = reports_dir / plan.plan_id
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / f"{report.started_at.strftime('%Y%m%dT%H%M%S%fZ')}-inventory.json"
    report.report_path = str(report_path)
    report_path.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def evaluate_inventory_run(
    *,
    repository: SQLiteRepository,
    run_id: str,
    brief: SearchBrief,
    retention_hours: int,
    evaluated_at: datetime | None = None,
    match_scope_id: str | None = None,
) -> InventoryEvaluationReport:
    """Evaluate one retained shared-inventory run without recollecting sources."""
    if retention_hours < 1:
        raise ValueError("retention_hours must be at least 1")
    evaluation_time = evaluated_at or datetime.now(UTC)
    evaluation_time = (
        evaluation_time.replace(tzinfo=UTC)
        if evaluation_time.tzinfo is None
        else evaluation_time.astimezone(UTC)
    )
    cutoff = evaluation_time - timedelta(hours=retention_hours)
    matched = 0
    needs_review = 0
    rejected = 0
    evaluated = 0
    match_rows: list[tuple[object, ...]] = []
    scoped_rows: list[tuple[object, ...]] = []
    with repository.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "SELECT DISTINCT j.id,j.payload_json,j.first_seen_at,"
            "e.posted_at AS retention_posted_at FROM inventory_run_jobs r "
            "JOIN jobs j ON j.id=r.job_id "
            "LEFT JOIN job_retention_evidence e ON e.job_id=j.id "
            "WHERE r.run_id=? AND j.lifecycle!='closed' "
            "ORDER BY j.id",
            (run_id,),
        )
        for row in cursor:
            job = Job.model_validate_json(row["payload_json"])
            retention_posted_at = (
                datetime.fromisoformat(row["retention_posted_at"])
                if row["retention_posted_at"]
                else None
            )
            basis = retention_basis(
                retention_posted_at=retention_posted_at,
                posted_at=job.posted_at,
                first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
            )
            if basis < cutoff:
                continue
            match = match_job(job, brief).model_copy(
                update={"evaluated_at": evaluation_time}
            )
            match_rows.append(repository._match_row(match))
            if match_scope_id is not None:
                scoped_rows.append(repository._scoped_match_row(match, match_scope_id))
            evaluated += 1
            if match.decision in {
                MatchDecision.STRONG_MATCH,
                MatchDecision.POSSIBLE_MATCH,
            }:
                matched += 1
            elif match.decision is MatchDecision.NEEDS_REVIEW:
                needs_review += 1
            else:
                rejected += 1
        if match_rows:
            connection.executemany(
                "INSERT OR REPLACE INTO job_matches VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                match_rows,
            )
        if scoped_rows:
            connection.executemany(
                "INSERT OR REPLACE INTO scoped_job_matches "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                scoped_rows,
            )

    return InventoryEvaluationReport(
        run_id=run_id,
        client_id=brief.client_id,
        evaluated_at=evaluation_time,
        total_evaluated=evaluated,
        total_matched=matched,
        total_needs_review=needs_review,
        total_rejected=rejected,
    )


def evaluate_recent_inventory(
    *,
    repository: SQLiteRepository,
    brief: SearchBrief,
    retention_hours: int = 72,
    evaluated_at: datetime | None = None,
    match_scope_id: str | None = None,
) -> tuple[InventoryEvaluationReport, tuple[str, ...]]:
    """Evaluate the currently retained shared inventory for one client.

    Collection is intentionally separate. Only active payloads whose shared
    retention basis is inside the retention window participate; posting freshness
    is still enforced later by daily-batch assembly using the SearchBrief policy.
    """
    if retention_hours < 1:
        raise ValueError("retention_hours must be at least 1")
    evaluation_time = evaluated_at or datetime.now(UTC)
    evaluation_time = (
        evaluation_time.replace(tzinfo=UTC)
        if evaluation_time.tzinfo is None
        else evaluation_time.astimezone(UTC)
    )
    cutoff = evaluation_time - timedelta(hours=retention_hours)
    matched = 0
    needs_review = 0
    rejected = 0
    evaluated = 0
    candidate_job_ids: list[str] = []
    match_rows: list[tuple[object, ...]] = []
    scoped_rows: list[tuple[object, ...]] = []
    with repository.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "SELECT j.id,j.payload_json,j.first_seen_at,"
            "e.posted_at AS retention_posted_at "
            "FROM jobs j LEFT JOIN job_retention_evidence e ON e.job_id=j.id "
            "WHERE j.lifecycle!='closed' "
            "ORDER BY j.id"
        )
        for row in cursor:
            job = Job.model_validate_json(row["payload_json"])
            retention_posted_at = (
                datetime.fromisoformat(row["retention_posted_at"])
                if row["retention_posted_at"]
                else None
            )
            basis = retention_basis(
                retention_posted_at=retention_posted_at,
                posted_at=job.posted_at,
                first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
            )
            if basis < cutoff:
                continue
            match = match_job(job, brief).model_copy(
                update={"evaluated_at": evaluation_time}
            )
            match_rows.append(repository._match_row(match))
            if match_scope_id is not None:
                scoped_rows.append(repository._scoped_match_row(match, match_scope_id))
            evaluated += 1
            if match.decision in {
                MatchDecision.STRONG_MATCH,
                MatchDecision.POSSIBLE_MATCH,
            }:
                matched += 1
                candidate_job_ids.append(job.id)
            elif match.decision is MatchDecision.NEEDS_REVIEW:
                needs_review += 1
                candidate_job_ids.append(job.id)
            else:
                rejected += 1
        if match_rows:
            connection.executemany(
                "INSERT OR REPLACE INTO job_matches VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                match_rows,
            )
        if scoped_rows:
            connection.executemany(
                "INSERT OR REPLACE INTO scoped_job_matches "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                scoped_rows,
            )

    scope_id = f"shared-inventory:{cutoff.isoformat()}:{evaluation_time.isoformat()}"
    return (
        InventoryEvaluationReport(
            run_id=scope_id,
            client_id=brief.client_id,
            evaluated_at=evaluation_time,
            total_evaluated=evaluated,
            total_matched=matched,
            total_needs_review=needs_review,
            total_rejected=rejected,
        ),
        tuple(candidate_job_ids),
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
    repository = create_repository(database_path)
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
