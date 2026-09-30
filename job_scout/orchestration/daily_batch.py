"""Globally assemble already-evaluated supply; never collect or invoke the matcher."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

from job_scout.dedupe.resolver import representative_key
from job_scout.delivery_destinations import (
    ClientSheetDestinationStore,
    parse_logical_destination,
)
from job_scout.domain.daily_batch import DailyBatchCounts, DailyBatchRequest, DailyBatchResult
from job_scout.domain.models import MatchDecision, UnknownEligibilityPolicy
from job_scout.export.batch_csv import destination_lock, file_digest, plan_csv, publish_csv
from job_scout.export.batch_sheets import (
    BatchSheetPublisher,
    ClientSheetPublisher,
    GoogleSheetsGateway,
    SheetsGateway,
    parse_sheet_destination,
)
from job_scout.export.csv_exporter import export_row
from job_scout.storage.daily_batches import DailyBatchStore
from job_scout.storage.sqlite import SQLiteRepository


def _timestamp(value) -> float:
    if value is None:
        return 0.0
    return value.replace(tzinfo=value.tzinfo or UTC).timestamp()


def _delivery_priority_key(candidate):
    decision_rank = 0 if candidate.match.decision is MatchDecision.STRONG_MATCH else 1
    score = candidate.match.score if candidate.match.score is not None else -1
    return (
        decision_rank,
        -score,
        -_timestamp(candidate.job.posted_at),
        -_timestamp(candidate.job.updated_at),
        representative_key(candidate.job),
    )


def _freshness_disposition(request, candidate) -> str | None:
    if request.max_posting_age_hours is None:
        return None
    posted_at = candidate.job.posted_at
    if posted_at is None:
        return (
            None
            if request.unknown_posting_age_policy is UnknownEligibilityPolicy.ALLOW
            else "posting_age_unknown"
        )
    evaluated_at = request.freshness_evaluated_at
    assert evaluated_at is not None
    evaluated_at = evaluated_at.replace(tzinfo=evaluated_at.tzinfo or UTC)
    posted_at = posted_at.replace(tzinfo=posted_at.tzinfo or UTC)
    age_seconds = (evaluated_at.astimezone(UTC) - posted_at.astimezone(UTC)).total_seconds()
    if age_seconds < -300:
        return "posting_time_invalid"
    if age_seconds > request.max_posting_age_hours * 3600:
        return "stale_posting"
    return None


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
    delivery_fresh_groups = {
        group: members
        for group, members in groups.items()
        if group not in historical | delivered
    }

    candidate_freshness = {
        v.job.id: _freshness_disposition(request, v)
        for members in delivery_fresh_groups.values()
        for v in members
    }
    qualified_members = {
        group: [v for v in members if candidate_freshness[v.job.id] is None]
        for group, members in delivery_fresh_groups.items()
    }
    suppressed_reason = {}
    for group, members in delivery_fresh_groups.items():
        if qualified_members[group]:
            continue
        reasons = {candidate_freshness[v.job.id] for v in members}
        suppressed_reason[group] = (
            "posting_time_invalid"
            if "posting_time_invalid" in reasons
            else "posting_age_unknown"
            if "posting_age_unknown" in reasons
            else "stale_posting"
        )

    stale_groups = {
        group for group, reason in suppressed_reason.items() if reason == "stale_posting"
    }
    unknown_age_groups = {
        group for group, reason in suppressed_reason.items() if reason == "posting_age_unknown"
    }
    invalid_time_groups = {
        group for group, reason in suppressed_reason.items() if reason == "posting_time_invalid"
    }

    fresh = {
        group: min(members, key=lambda v: representative_key(v.job))
        for group, members in qualified_members.items()
        if members
    }

    cooldown_groups = {
        group for group, candidate in fresh.items() if candidate.employer_recently_delivered
    }
    available = {
        group: candidate for group, candidate in fresh.items() if group not in cooldown_groups
    }
    available_employers = {candidate.employer_key for candidate in available.values()}

    company_cap_groups: set[str] = set()
    if request.max_jobs_per_employer_per_batch is None:
        ordered_pool = [(group, available[group]) for group in sorted(available)]
    else:
        by_employer = {}
        for group, candidate in available.items():
            by_employer.setdefault(candidate.employer_key, []).append((group, candidate))
        for members in by_employer.values():
            members.sort(key=lambda item: (_delivery_priority_key(item[1]), item[0]))

        cap = request.max_jobs_per_employer_per_batch
        retained_by_employer = {}
        for employer, members in by_employer.items():
            retained_by_employer[employer] = members[:cap]
            company_cap_groups.update(group for group, _ in members[cap:])

        employer_order = sorted(
            retained_by_employer,
            key=lambda employer: (
                _delivery_priority_key(retained_by_employer[employer][0][1]),
                employer,
            ),
        )
        ordered_pool = []
        for position in range(cap):
            for employer in employer_order:
                members = retained_by_employer[employer]
                if position < len(members):
                    ordered_pool.append(members[position])

    selected_pairs = ordered_pool[: request.requested_quota]
    selected = [candidate for _, candidate in selected_pairs]
    selected_jobs = {v.job.id for v in selected}
    selected_groups = {group for group, _ in selected_pairs}

    dispositions = {}
    for v in candidates:
        freshness_reason = candidate_freshness.get(v.job.id)
        dispositions[v.job.id] = (
            v.match.decision.value
            if v.match.decision not in {MatchDecision.STRONG_MATCH, MatchDecision.POSSIBLE_MATCH}
            else "historical"
            if v.group_id in historical
            else "prior_delivery"
            if v.group_id in delivered
            else freshness_reason
            if freshness_reason is not None
            else "employer_cooldown"
            if v.group_id in cooldown_groups
            else "company_duplicate_in_batch"
            if v.group_id in company_cap_groups
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
        fresh_eligible_groups=len(delivery_fresh_groups),
        fresh_eligible_employers=len(available_employers),
        employer_cooldown_suppressed_groups=len(cooldown_groups),
        stale_posting_suppressed_groups=len(stale_groups),
        unknown_age_suppressed_groups=len(unknown_age_groups),
        invalid_time_suppressed_groups=len(invalid_time_groups),
        company_cap_suppressed_groups=len(company_cap_groups),
        selected_groups=len(selected),
    )
    return counts, selected, dispositions, [export_row(v.job) for v in selected]


def prepare_daily_batch(
    *,
    repository: SQLiteRepository,
    request: DailyBatchRequest,
) -> DailyBatchResult:
    # CSV paths resolve as in run_pipeline; Sheets destinations have stable URI identity.
    destination = request.destination
    if destination.startswith("gsheet:"):
        parse_sheet_destination(destination)
    elif destination.startswith("client-sheet:"):
        destination_id = parse_logical_destination(destination)
        if request.destination_id != destination_id:
            raise ValueError("client destination ID does not match destination URI")
        if request.destination_config_sha256 is None:
            raise ValueError("client destination requires a frozen config hash")
    elif "://" in destination:
        raise ValueError("unsupported batch destination")
    else:
        destination = str(Path(destination).resolve())
    request = DailyBatchRequest.model_validate({**request.model_dump(), "destination": destination})
    return DailyBatchStore(repository).prepare(request, _assemble)


def finalize_daily_batch(
    *, repository: SQLiteRepository, batch_id: str, sheets_gateway: SheetsGateway | None = None
) -> DailyBatchResult:
    store = DailyBatchStore(repository)
    result = store.get(batch_id)
    if result.status == "delivered":
        return result
    if result.request.destination.startswith("gsheet:"):
        try:
            publisher = BatchSheetPublisher(result, sheets_gateway or GoogleSheetsGateway())
            return store.finalize(batch_id, publisher.plan, publisher.inspect, publisher.publish)
        except OSError as error:
            return store.fail(batch_id, error)
    if result.request.destination.startswith("client-sheet:"):
        try:
            destination_id = parse_logical_destination(result.request.destination)
            destination = ClientSheetDestinationStore(repository).get(
                result.request.client_id, destination_id
            )
            publisher = ClientSheetPublisher(
                result,
                sheets_gateway or GoogleSheetsGateway(),
                destination,
            )
            return store.finalize(batch_id, publisher.plan, publisher.inspect, publisher.publish)
        except OSError as error:
            return store.fail(batch_id, error)
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
