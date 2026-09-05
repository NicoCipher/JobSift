from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from job_scout.collectors.base import JobCollector
from job_scout.domain.models import (
    CandidateProfile,
    CollectionStatus,
    JobLifecycle,
    MatchDecision,
    SearchBrief,
    SourceTarget,
)
from job_scout.export.csv_exporter import write_csv
from job_scout.matching.matcher import match_job
from job_scout.storage.sqlite import SQLiteRepository


@dataclass(frozen=True)
class PipelineSummary:
    received: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    matched: int = 0
    rejected: int = 0
    exported: int = 0
    status: str = "success"


def run_pipeline(
    *,
    collector: JobCollector,
    target: SourceTarget,
    profile: SearchBrief | CandidateProfile,
    repository: SQLiteRepository,
    csv_path: str | Path,
) -> PipelineSummary:
    result = collector.collect(target)
    if result.status not in {CollectionStatus.SUCCESS, CollectionStatus.PARTIAL}:
        return PipelineSummary(status=result.status.value)
    counters = {"new": 0, "changed": 0, "unchanged": 0, "matched": 0, "rejected": 0}
    exportable = []
    destination = str(Path(csv_path).resolve())
    for job in result.jobs:
        state = repository.upsert_job(job)
        counters[
            "new"
            if state is JobLifecycle.NEW
            else "changed"
            if state is JobLifecycle.CHANGED
            else "unchanged"
        ] += 1
        match = match_job(job, profile)
        repository.save_match(match)
        if match.decision in {MatchDecision.STRONG_MATCH, MatchDecision.POSSIBLE_MATCH}:
            counters["matched"] += 1
            if not repository.is_exported(job.id, profile.client_id, destination):
                exportable.append(job)
        else:
            counters["rejected"] += 1
    exported = write_csv(csv_path, exportable)
    for job in exportable:
        repository.mark_exported(job.id, profile.client_id, destination)
    return PipelineSummary(
        received=len(result.jobs), exported=exported, status=result.status.value, **counters
    )
