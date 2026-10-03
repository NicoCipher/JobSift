"""Production Workday index-first collection.

The production adapter uses the read-only Workday list index to avoid hydrating an
entire board when only a small client-relevant subset can pass the deterministic
matcher title gate. Negative filtering is allowed only after complete index
coverage. Incomplete index evidence falls back to the legacy full collector.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from job_scout.collectors.workday import WorkdayCollector
from job_scout.delivery_profiles import ClientDeliveryProfileStore
from job_scout.domain.models import (
    CollectionResult,
    CollectionStatus,
    Job,
    SearchBrief,
    SourceTarget,
)
from job_scout.production_registry import ProductionSourceRegistry, ProductionTarget
from job_scout.search_brief import load_search_brief
from job_scout.sourcing_plan import load_sourcing_plan
from job_scout.workday_index import (
    WorkdayIndexScanner,
    definitely_older_than_72h,
    role_title_candidate,
)


class WorkdayBriefBinding(BaseModel):
    """Immutable repository binding for one active profile SearchBrief."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    sha256: str



class WorkdayRetainedCandidateBinding(BaseModel):
    """Retained Workday provider identities that could still match an active brief."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    board_id: str
    source_job_ids: list[str]

def resolve_retained_workday_candidate_bindings(
    *,
    repository,
    briefs: list[SearchBrief],
) -> list[WorkdayRetainedCandidateBinding]:
    """Freeze only retained Workday identities whose stored title can still match."""
    if not briefs:
        return []

    grouped: dict[str, set[str]] = defaultdict(set)
    with repository.connect() as connection:
        rows = connection.execute(
            "SELECT source_board_id,source_job_id,payload_json FROM jobs "
            "WHERE source='workday' AND lifecycle!='closed' "
            "ORDER BY source_board_id,source_job_id"
        ).fetchall()

    for row in rows:
        job = Job.model_validate_json(row["payload_json"])
        if (
            job.source != "workday"
            or job.source_board_id != row["source_board_id"]
            or job.source_job_id != row["source_job_id"]
        ):
            raise ValueError("retained Workday job provenance is inconsistent")
        if any(role_title_candidate(job.title, brief) for brief in briefs):
            grouped[job.source_board_id].add(job.source_job_id)

    return [
        WorkdayRetainedCandidateBinding(
            board_id=board_id,
            source_job_ids=sorted(source_job_ids),
        )
        for board_id, source_job_ids in sorted(grouped.items())
    ]


def _repo_relative(path: Path, repo_root: Path) -> str:
    resolved_root = repo_root.resolve()
    resolved = path.resolve()
    try:
        return resolved.relative_to(resolved_root).as_posix()
    except ValueError as exc:
        raise ValueError("active SearchBrief must live inside the repository") from exc


def resolve_active_workday_brief_bindings(
    *,
    repository,
    plan_dir: Path,
    repo_root: Path,
) -> list[WorkdayBriefBinding]:
    """Bind active delivery profiles to exact SearchBrief repository bytes."""

    profiles = ClientDeliveryProfileStore(repository).active()
    if not profiles:
        return []

    plans_by_id: dict[str, list[tuple[Path, object]]] = {}
    for path in sorted(plan_dir.glob("*.json")):
        plan = load_sourcing_plan(path)
        plans_by_id.setdefault(plan.plan_id, []).append((path.resolve(), plan))

    bindings: dict[str, WorkdayBriefBinding] = {}
    for profile in profiles:
        matches = plans_by_id.get(profile.sourcing_plan_id, [])
        if len(matches) != 1:
            raise ValueError(
                "expected exactly one sourcing plan for active profile "
                f"{profile.sourcing_plan_id!r}; found {len(matches)}"
            )
        plan_path, plan = matches[0]
        brief_path = (plan_path.parent / plan.search_brief).resolve()
        brief = load_search_brief(brief_path)
        if brief.client_id != profile.client_id:
            raise ValueError(
                "active delivery profile client does not match its sourcing-plan SearchBrief"
            )
        relative = _repo_relative(brief_path, repo_root)
        digest = hashlib.sha256(brief_path.read_bytes()).hexdigest()
        binding = WorkdayBriefBinding(path=relative, sha256=digest)
        existing = bindings.get(relative)
        if existing is not None and existing != binding:
            raise ValueError("active SearchBrief path resolved to conflicting bytes")
        bindings[relative] = binding

    return [bindings[path] for path in sorted(bindings)]


def verify_active_workday_brief_bindings(
    *,
    repository,
    plan_dir: Path,
    repo_root: Path,
    expected: list[WorkdayBriefBinding],
) -> None:
    """Fail closed when active client coverage changed after refresh planning."""

    current = resolve_active_workday_brief_bindings(
        repository=repository,
        plan_dir=plan_dir,
        repo_root=repo_root,
    )
    if current != expected:
        raise ValueError(
            "active Workday SearchBrief snapshot changed after refresh planning"
        )


def load_bound_workday_briefs(
    bindings: list[WorkdayBriefBinding],
    *,
    repo_root: Path,
) -> list[SearchBrief]:
    """Load only the SearchBrief bytes frozen into the refresh plan."""

    briefs: list[SearchBrief] = []
    root = repo_root.resolve()
    for binding in bindings:
        path = (root / binding.path).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("Workday SearchBrief binding escapes the repository") from exc
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != binding.sha256:
            raise ValueError(f"Workday SearchBrief binding changed after planning: {binding.path}")
        briefs.append(load_search_brief(path))
    return briefs


class IndexFirstWorkdayCollector:
    """Collect one Workday target with complete index coverage before hydration."""

    source = "workday"

    def __init__(
        self,
        *,
        registry: ProductionSourceRegistry,
        briefs: list[SearchBrief],
        detail_concurrency: int,
        scanner_factory: Callable[[], WorkdayIndexScanner] = WorkdayIndexScanner,
        hydration_factory: Callable[[int], WorkdayCollector] | None = None,
    ) -> None:
        self.briefs = list(briefs)
        self.detail_concurrency = detail_concurrency
        self.scanner_factory = scanner_factory
        self.hydration_factory = hydration_factory or (
            lambda concurrency: WorkdayCollector(detail_concurrency=concurrency)
        )
        self.targets_by_board: dict[str, ProductionTarget] = {}
        for production_target in registry.targets:
            if production_target.source != "workday":
                continue
            board_id = production_target.source_target().board_id
            if board_id in self.targets_by_board:
                raise ValueError("duplicate Workday board in refresh registry")
            self.targets_by_board[board_id] = production_target
        self.last_counts: dict[str, int | float | str | bool | None] = {}

    @staticmethod
    def _close(value) -> None:
        close = getattr(value, "close", None)
        if callable(close):
            close()
            return
        client = getattr(value, "client", None)
        if client is not None:
            client.close()

    def _hydrator(self) -> WorkdayCollector:
        return self.hydration_factory(self.detail_concurrency)

    def collect(self, target: SourceTarget) -> CollectionResult:
        production_target = self.targets_by_board.get(target.board_id)
        if production_target is None:
            raise ValueError("Workday refresh target is outside the planned production registry")

        scanner = self.scanner_factory()
        try:
            index = scanner.scan(production_target)
        finally:
            self._close(scanner)

        index_counts = {
            "collection_mode": "index_first",
            "active_briefs": len(self.briefs),
            "index_status": index.status.value,
            "index_broad_total": index.broad_total,
            "index_provider_rows_seen": index.provider_rows_seen,
            "index_unique_postings": len(index.postings),
            "index_coverage_mode": index.coverage_mode,
        }

        if index.status is not CollectionStatus.SUCCESS:
            # Negative filtering is unsafe when list coverage is incomplete. Preserve
            # the old semantics instead of silently losing unseen candidate paths.
            hydrator = self._hydrator()
            try:
                result = hydrator.collect(target)
                details = getattr(hydrator, "last_counts", {})
            finally:
                self._close(hydrator)
            self.last_counts = {
                **index_counts,
                "fallback_full_collection": True,
                "index_errors": len(index.errors),
                **(details if isinstance(details, dict) else {}),
            }
            return result

        candidate_paths: list[str] = []
        stale_skipped = 0
        title_skipped = 0
        uncertain_title_hydrated = 0

        for posting in index.postings:
            if definitely_older_than_72h(posting.posted_on):
                stale_skipped += 1
                continue
            if not self.briefs:
                title_skipped += 1
                continue
            if posting.title is None:
                uncertain_title_hydrated += 1
                candidate_paths.append(posting.external_path)
                continue
            if any(role_title_candidate(posting.title, brief) for brief in self.briefs):
                candidate_paths.append(posting.external_path)
            else:
                title_skipped += 1

        if not candidate_paths:
            self.last_counts = {
                **index_counts,
                "fallback_full_collection": False,
                "index_stale_skipped": stale_skipped,
                "index_title_skipped": title_skipped,
                "uncertain_title_hydrated": uncertain_title_hydrated,
                "candidate_paths": 0,
                "detail_attempts": 0,
                "normalized": 0,
            }
            return CollectionResult(
                source=self.source,
                target=target,
                status=CollectionStatus.SUCCESS,
                raw_postings_received=len(index.postings),
            )

        hydrator = self._hydrator()
        try:
            result = hydrator.hydrate_paths(target, candidate_paths)
            details = getattr(hydrator, "last_counts", {})
        finally:
            self._close(hydrator)

        self.last_counts = {
            **index_counts,
            "fallback_full_collection": False,
            "index_stale_skipped": stale_skipped,
            "index_title_skipped": title_skipped,
            "uncertain_title_hydrated": uncertain_title_hydrated,
            "candidate_paths": len(candidate_paths),
            **(details if isinstance(details, dict) else {}),
        }
        return CollectionResult(
            source=result.source,
            target=result.target,
            status=result.status,
            jobs=result.jobs,
            errors=result.errors,
            raw_postings_received=len(index.postings),
        )
