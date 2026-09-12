"""Globally assemble already-evaluated supply; never collect or invoke the matcher."""

from __future__ import annotations

from pathlib import Path

from job_scout.dedupe.resolver import representative_key
from job_scout.domain.daily_batch import DailyBatchCounts, DailyBatchRequest, DailyBatchResult
from job_scout.domain.models import MatchDecision
from job_scout.export.batch_csv import destination_lock, file_digest, plan_csv, publish_csv
from job_scout.export.csv_exporter import export_row
from job_scout.storage.daily_batches import DailyBatchStore
from job_scout.storage.sqlite import SQLiteRepository


def _assemble(request, candidates):
    eligible = [
        v
        for v in candidates
        if v.match.decision in {MatchDecision.STRONG_MATCH, MatchDecision.POSSIBLE_MATCH}
    ]
    groups = {}
    for v in eligible:
        groups.setdefault(v.group_id, []).append(v)
    historical = {group for group, members in groups.items() if any(v.historical for v in members)}
    delivered = {
        group
        for group, members in groups.items()
        if group not in historical and any(v.delivered for v in members)
    }
    fresh = {
        group: min(members, key=lambda v: representative_key(v.job))
        for group, members in groups.items()
        if group not in historical | delivered
    }
    # Match priority is not new ranking: retain the existing sorted persistent-group order.
    selected = [fresh[group] for group in sorted(fresh)[: request.requested_quota]]
    selected_jobs = {v.job.id for v in selected}
    selected_groups = {v.group_id for v in selected}
    dispositions = {}
    for v in candidates:
        dispositions[v.job.id] = (
            v.match.decision.value
            if v.match.decision not in {MatchDecision.STRONG_MATCH, MatchDecision.POSSIBLE_MATCH}
            else "historical"
            if v.group_id in historical
            else "prior_delivery"
            if v.group_id in delivered
            else "selected_representative"
            if v.job.id in selected_jobs
            else "practical_duplicate"
            if v.group_id in selected_groups
            else "outside_quota"
        )
    counts = DailyBatchCounts(
        candidate_postings=len(candidates),
        match_eligible_postings=len(eligible),
        needs_review_postings=sum(
            v.match.decision is MatchDecision.NEEDS_REVIEW for v in candidates
        ),
        rejected_postings=sum(v.match.decision is MatchDecision.REJECT for v in candidates),
        match_eligible_groups=len(groups),
        historically_suppressed_groups=len(historical),
        previously_delivered_groups=len(delivered),
        duplicate_postings_collapsed=len(eligible) - len(groups),
        fresh_eligible_groups=len(fresh),
        selected_groups=len(selected),
    )
    return counts, selected, dispositions, [export_row(v.job) for v in selected]


def prepare_daily_batch(
    *,
    repository: SQLiteRepository,
    request: DailyBatchRequest,
) -> DailyBatchResult:
    # Canonical destination is the same path identity used by run_pipeline.
    request = DailyBatchRequest.model_validate(
        {**request.model_dump(), "destination": str(Path(request.destination).resolve())}
    )
    return DailyBatchStore(repository).prepare(request, _assemble)


def finalize_daily_batch(*, repository: SQLiteRepository, batch_id: str) -> DailyBatchResult:
    store = DailyBatchStore(repository)
    result = store.get(batch_id)
    if result.status == "delivered":
        return result
    path = Path(result.request.destination)
    try:
        with destination_lock(path):
            return store.finalize(
                batch_id,
                lambda rows: plan_csv(path, rows),
                lambda: file_digest(path),
                lambda rows, before, after: publish_csv(path, rows, before, after),
            )
    except OSError as error:
        return store.fail(batch_id, error)
