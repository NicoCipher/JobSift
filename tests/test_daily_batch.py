import csv
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from job_scout.cli import main
from job_scout.domain.daily_batch import BatchConflict, DailyBatchRequest
from job_scout.domain.models import Job, JobMatch
from job_scout.export.batch_sheets import SHEET_COLUMNS, sheet_destination
from job_scout.history import HistoricalBlacklistEvidence, HistoricalRecord
from job_scout.orchestration import daily_batch as batches
from job_scout.storage.daily_batches import DailyBatchStore
from job_scout.storage.sqlite import SQLiteRepository

CLIENT = "taiwo_operator_sourcing_v1"
NOW = datetime(2026, 9, 1, tzinfo=UTC)


def posting(n, **changes):
    values = {
        "id": f"job-{n}",
        "source": "greenhouse",
        "source_board_id": "acme",
        "source_job_id": str(n),
        "title": "Support Engineer",
        "company": "Acme",
        "description_text": f"Vacancy {n}",
        "job_url": f"https://example.com/jobs/{n}",
        "canonical_url": f"https://example.com/jobs/{n}",
        "content_fingerprint": str(n),
        "discovered_at": NOW,
        "last_seen_at": NOW,
    }
    values.update(changes)
    return Job(**values)


def seed(repo, jobs, decisions=None, client=CLIENT):
    for job, decision in zip(jobs, decisions or ["strong_match"] * len(jobs)):
        repo.upsert_job(job)
        repo.save_match(
            JobMatch(
                job_id=job.id,
                client_id=client,
                decision=decision,
                evaluated_at=NOW,
                matcher_version="matcher-v1",
            )
        )


def request(repo, path, jobs, quota=5, **changes):
    ids = tuple(j.id for j in jobs)
    values = {
        "client_id": CLIENT,
        "destination": str(path),
        "idempotency_key": "day-1",
        "requested_quota": quota,
        "evidence_scope_id": "fixture-scope",
        "evaluation_id": "eval-1",
        "candidate_job_ids": ids,
        "evidence_sha256": DailyBatchStore(repo).evidence_digest(CLIENT, ids),
    }
    values.update(changes)
    return DailyBatchRequest(**values)


def prepare(repo, req):
    return batches.prepare_daily_batch(repository=repo, request=req)


def finalize(repo, result):
    return batches.finalize_daily_batch(repository=repo, batch_id=result.batch_id)


def rows(path):
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def history(repo, job, status="unknown", client=CLIENT):
    repo.import_historical_records(
        client_id=client,
        workbook_sha256="a" * 64,
        records=[
            HistoricalRecord(
                str(job.canonical_url),
                str(job.canonical_url),
                job.source,
                job.source_board_id,
                job.source_job_id,
                job.title,
                job.company,
                status,
                "History",
                2,
            )
        ],
    )


@pytest.fixture
def repo(tmp_path):
    return SQLiteRepository(tmp_path / "jobs.db")


@pytest.mark.parametrize(
    "quota,supply,selected,shortfall",
    [(3, 3, 3, 0), (5, 3, 3, 2), (2, 5, 2, 0), (4, 0, 0, 4), (150, 87, 87, 63)],
)
def test_quota(repo, tmp_path, quota, supply, selected, shortfall):
    jobs = [posting(n) for n in range(supply)]
    seed(repo, jobs)
    result = prepare(repo, request(repo, tmp_path / "out.csv", jobs, quota))
    assert (result.selected_count, result.shortfall) == (selected, shortfall)
    assert result.counts.fresh_eligible_groups == supply
    delivered = finalize(repo, result)
    assert delivered.status == "delivered"
    assert len(rows(tmp_path / "out.csv")) == selected


def test_batch_freshness_gate_suppresses_stale_unknown_and_future_postings(repo, tmp_path):
    jobs = [
        posting(1, posted_at=NOW - timedelta(hours=23)),
        posting(2, posted_at=NOW - timedelta(hours=25)),
        posting(3, posted_at=None),
        posting(4, posted_at=NOW + timedelta(hours=2)),
    ]
    seed(repo, jobs)
    req = request(
        repo,
        tmp_path / "out.csv",
        jobs,
        quota=4,
        max_posting_age_hours=24,
        unknown_posting_age_policy="reject",
        freshness_evaluated_at=NOW,
    )

    result = prepare(repo, req)

    assert result.selected_count == 1
    assert result.shortfall == 3
    assert result.counts.stale_posting_suppressed_groups == 1
    assert result.counts.unknown_age_suppressed_groups == 1
    assert result.counts.invalid_time_suppressed_groups == 1
    assert result.items[0].representative_job_id == jobs[0].id

    with repo.connect() as connection:
        dispositions = {
            row["job_id"]: row["disposition"]
            for row in connection.execute(
                "SELECT job_id,disposition FROM daily_batch_candidates WHERE batch_id=?",
                (result.batch_id,),
            ).fetchall()
        }
    assert dispositions[jobs[0].id] == "selected_representative"
    assert dispositions[jobs[1].id] == "stale_posting"
    assert dispositions[jobs[2].id] == "posting_age_unknown"
    assert dispositions[jobs[3].id] == "posting_time_invalid"


def test_re_evaluation_timestamp_does_not_invalidate_prepared_match_evidence(repo, tmp_path):
    job = posting(1)
    seed(repo, [job])
    prepared = prepare(repo, request(repo, tmp_path / "out.csv", [job], quota=1))

    repo.save_match(
        JobMatch(
            job_id=job.id,
            client_id=CLIENT,
            decision="strong_match",
            evaluated_at=NOW + timedelta(hours=1),
            matcher_version="matcher-v1",
        )
    )

    delivered = finalize(repo, prepared)
    assert delivered.status == "delivered"
    assert len(rows(tmp_path / "out.csv")) == 1


def test_failed_unpublished_batch_is_discardable(repo, tmp_path):
    job = posting(1)
    seed(repo, [job])
    prepared = prepare(repo, request(repo, tmp_path / "out.csv", [job], quota=1))
    failed = DailyBatchStore(repo).fail(prepared.batch_id, "posting expired before release")

    assert failed.status == "failed"
    discarded = DailyBatchStore(repo).discard_prepared(
        failed.batch_id, expected_generation_id=failed.generation_id
    )
    assert discarded.status == "failed"
    with pytest.raises(BatchConflict, match="batch not found"):
        DailyBatchStore(repo).get(failed.batch_id)


def test_discard_prepared_batch_allows_safe_replacement(repo, tmp_path):
    job = posting(1)
    seed(repo, [job])
    req = request(repo, tmp_path / "out.csv", [job])
    prepared = prepare(repo, req)

    discarded = DailyBatchStore(repo).discard_prepared(
        prepared.batch_id, expected_generation_id=prepared.generation_id
    )
    assert discarded.batch_id == prepared.batch_id
    assert discarded.status == "prepared"
    with pytest.raises(BatchConflict, match="batch not found"):
        DailyBatchStore(repo).get(prepared.batch_id)

    replacement = prepare(repo, req)
    assert replacement.status == "prepared"
    assert replacement.batch_id == prepared.batch_id

    delivered = finalize(repo, replacement)
    assert delivered.status == "delivered"
    with pytest.raises(BatchConflict, match="only an unpublished batch"):
        DailyBatchStore(repo).discard_prepared(
            delivered.batch_id, expected_generation_id=delivered.generation_id
        )


def test_company_cap_returns_distinct_employers_and_honest_shortfall(repo, tmp_path):
    jobs = [
        posting(1, company="Reddit", employer_id="reddit"),
        posting(2, company="Reddit", employer_id="reddit"),
        posting(3, company="Reddit", employer_id="reddit"),
        posting(4, company="GitLab", employer_id="gitlab"),
        posting(5, company="Render", employer_id="render"),
    ]
    seed(repo, jobs)
    req = request(
        repo,
        tmp_path / "out.csv",
        jobs,
        quota=5,
        max_jobs_per_employer_per_batch=1,
    )
    result = prepare(repo, req)

    assert result.selected_count == 3
    assert result.shortfall == 2
    assert result.counts.fresh_eligible_employers == 3
    assert result.counts.company_cap_suppressed_groups == 2

    selected_companies = {row["Company Name"] for row in DailyBatchStore(repo).export_rows(result.batch_id)}
    assert selected_companies == {"Reddit", "GitLab", "Render"}

    with repo.connect() as connection:
        dispositions = {
            row[0]
            for row in connection.execute(
                "SELECT disposition FROM daily_batch_candidates WHERE batch_id=?",
                (result.batch_id,),
            ).fetchall()
        }
    assert "company_duplicate_in_batch" in dispositions


def test_company_cap_is_configurable_and_diversity_precedes_second_slot(repo, tmp_path):
    jobs = [
        posting(1, company="Alpha", employer_id="alpha"),
        posting(2, company="Alpha", employer_id="alpha"),
        posting(3, company="Alpha", employer_id="alpha"),
        posting(4, company="Beta", employer_id="beta"),
    ]
    seed(repo, jobs)
    result = prepare(
        repo,
        request(
            repo,
            tmp_path / "out.csv",
            jobs,
            quota=3,
            max_jobs_per_employer_per_batch=2,
        ),
    )
    companies = [row["Company Name"] for row in DailyBatchStore(repo).export_rows(result.batch_id)]

    assert result.selected_count == 3
    assert companies.count("Alpha") == 2
    assert companies.count("Beta") == 1
    assert companies[:2] == ["Alpha", "Beta"]


def test_employer_cooldown_suppresses_new_role_without_rejecting_it(repo, tmp_path):
    destination = tmp_path / "out.csv"
    first = posting(1, company="Reddit", employer_id="reddit")
    seed(repo, [first])
    delivered = finalize(
        repo,
        prepare(
            repo,
            request(
                repo,
                destination,
                [first],
                quota=1,
                max_jobs_per_employer_per_batch=1,
            ),
        ),
    )
    assert delivered.status == "delivered"

    second = posting(2, company="Reddit", employer_id="reddit")
    seed(repo, [second])
    req = request(
        repo,
        destination,
        [second],
        quota=1,
        idempotency_key="day-2",
        max_jobs_per_employer_per_batch=1,
        employer_cooldown_days=30,
    )
    result = prepare(repo, req)

    assert result.selected_count == 0
    assert result.shortfall == 1
    assert result.counts.employer_cooldown_suppressed_groups == 1
    with repo.connect() as connection:
        disposition = connection.execute(
            "SELECT disposition FROM daily_batch_candidates WHERE batch_id=? AND job_id=?",
            (result.batch_id, second.id),
        ).fetchone()[0]
    assert disposition == "employer_cooldown"


def test_operator_reviews_frozen_batch_before_explicit_release(repo, tmp_path, monkeypatch, capsys):
    job = posting(1)
    seed(repo, [job])
    destination = tmp_path / "out.csv"
    prepared = prepare(repo, request(repo, destination, [job], quota=2))

    def command(*args):
        monkeypatch.setattr(sys, "argv", ["job-scout", "batch", *args])
        main()
        return json.loads(capsys.readouterr().out)

    reviewed = command("review", "--database", str(repo.path), "--batch-id", prepared.batch_id)
    assert reviewed["status"] == "prepared"
    assert (reviewed["selected_count"], reviewed["shortfall"]) == (1, 1)
    assert reviewed["rows"][0]["Job Link"] == str(job.canonical_url)
    assert not destination.exists()

    # Reviewing an existing snapshot must not run repository initialization.
    original_init = SQLiteRepository.__init__

    def reject_init(self, path):
        raise AssertionError("review initialized the repository")

    monkeypatch.setattr(SQLiteRepository, "__init__", reject_init)
    assert (
        command("review", "--database", str(repo.path), "--batch-id", prepared.batch_id)["status"]
        == "prepared"
    )
    monkeypatch.setattr(SQLiteRepository, "__init__", original_init)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "job-scout",
            "batch",
            "release",
            "--database",
            str(repo.path),
            "--batch-id",
            prepared.batch_id,
            "--confirm-batch-id",
            "wrong",
        ],
    )
    monkeypatch.setattr(SQLiteRepository, "__init__", reject_init)
    with pytest.raises(SystemExit):
        main()
    monkeypatch.setattr(SQLiteRepository, "__init__", original_init)
    capsys.readouterr()
    assert not destination.exists()

    released = command(
        "release",
        "--database",
        str(repo.path),
        "--batch-id",
        prepared.batch_id,
        "--confirm-batch-id",
        prepared.batch_id,
    )
    assert released["status"] == "delivered"
    assert len(rows(destination)) == 1
    assert (
        command(
            "release",
            "--database",
            str(repo.path),
            "--batch-id",
            prepared.batch_id,
            "--confirm-batch-id",
            prepared.batch_id,
        )["status"]
        == "delivered"
    )
    assert len(rows(destination)) == 1


def test_operator_release_failure_exits_nonzero_with_failure_details(
    repo, tmp_path, monkeypatch, capsys
):
    job = posting(1)
    seed(repo, [job])
    prepared = prepare(repo, request(repo, tmp_path / "out.csv", [job]))
    # The frozen selection is no longer eligible at publication time.
    seed(repo, [job], ["reject"])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "job-scout",
            "batch",
            "release",
            "--database",
            str(repo.path),
            "--batch-id",
            prepared.batch_id,
            "--confirm-batch-id",
            prepared.batch_id,
        ],
    )
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "failed"
    assert output["error"]
    assert not (tmp_path / "out.csv").exists()


def test_review_missing_database_does_not_create_it(tmp_path, monkeypatch):
    missing = tmp_path / "missing.sqlite3"
    monkeypatch.setattr(
        sys,
        "argv",
        ["job-scout", "batch", "review", "--database", str(missing), "--batch-id", "unknown"],
    )
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 2
    assert not missing.exists()


class FakeSheets:
    def __init__(self):
        self.values = [SHEET_COLUMNS.copy()]
        self.uncertain = False
        self.fail_before = False
        self.append_calls = 0

    def read_rows(self, spreadsheet_id, tab):
        assert (spreadsheet_id, tab) == ("example123", "Sheet1")
        return [row.copy() for row in self.values]

    def append_rows(self, spreadsheet_id, tab, rows):
        assert (spreadsheet_id, tab) == ("example123", "Sheet1")
        self.append_calls += 1
        if self.fail_before:
            self.fail_before = False
            raise OSError("append never reached Sheets")
        self.values.extend([row.copy() for row in rows])
        if self.uncertain:
            self.uncertain = False
            raise OSError("response lost after commit")


def test_sheet_release_recovers_uncertain_append_and_preserves_status(repo):
    jobs = [posting(1), posting(2)]
    seed(repo, jobs)
    destination = sheet_destination("example123", "Sheet1")
    batch = prepare(repo, request(repo, destination, jobs))
    assert batch.request.destination == destination
    gateway = FakeSheets()
    gateway.uncertain = True
    failed = batches.finalize_daily_batch(
        repository=repo, batch_id=batch.batch_id, sheets_gateway=gateway
    )
    assert failed.status == "failed"
    assert gateway.append_calls == 1
    assert len(gateway.values) == 3
    gateway.values[1][5] = "Applied"
    recovered = batches.finalize_daily_batch(
        repository=repo, batch_id=batch.batch_id, sheets_gateway=gateway
    )
    assert recovered.status == "delivered"
    assert gateway.append_calls == 1
    assert gateway.values[1][5] == "Applied"
    assert {row[6] for row in gateway.values[1:]} == {batch.batch_id}
    assert {row[8] for row in gateway.values[1:]} == {job.id for job in jobs}
    assert (
        prepare(repo, request(repo, destination, jobs, idempotency_key="next-day")).selected_count
        == 0
    )


def test_legacy_sheet_freshness_conflict_is_persisted_as_failed(repo):
    job = posting(1, posted_at=NOW - timedelta(hours=23))
    seed(repo, [job])
    destination = sheet_destination("example123", "Sheet1")
    batch = prepare(
        repo,
        request(
            repo,
            destination,
            [job],
            max_posting_age_hours=24,
            freshness_evaluated_at=NOW,
        ),
    )
    assert batch.status == "prepared"

    with repo.connect() as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM jobs WHERE id=?", (job.id,)
            ).fetchone()[0]
        )
        payload["posted_at"] = (NOW - timedelta(hours=25)).isoformat()
        connection.execute(
            "UPDATE jobs SET payload_json=? WHERE id=?",
            (json.dumps(payload), job.id),
        )

    failed = batches.finalize_daily_batch(
        repository=repo,
        batch_id=batch.batch_id,
        sheets_gateway=FakeSheets(),
    )

    assert failed.status == "failed"
    assert failed.error == (
        "prepared posting is no longer fresh at delivery: stale_posting"
    )
    assert DailyBatchStore(repo).export_journal(batch.batch_id) == (None, None)


def test_sheet_header_and_drift_fail_closed(repo):
    job = posting(1)
    seed(repo, [job])
    destination = sheet_destination("example123", "Sheet1")
    batch = prepare(repo, request(repo, destination, [job]))
    gateway = FakeSheets()
    gateway.values[0][2] = "Wrong"
    with pytest.raises(BatchConflict, match="header"):
        batches.finalize_daily_batch(
            repository=repo, batch_id=batch.batch_id, sheets_gateway=gateway
        )
    assert DailyBatchStore(repo).get(batch.batch_id).status == "prepared"
    assert gateway.append_calls == 0

    gateway.values[0][2] = "Job Link"
    gateway.fail_before = True
    assert (
        batches.finalize_daily_batch(
            repository=repo, batch_id=batch.batch_id, sheets_gateway=gateway
        ).status
        == "failed"
    )

    # An external edit after journaling cannot be silently overwritten.
    gateway.values.append(["manual", "", "https://example.com/manual"])
    retry = batches.finalize_daily_batch(
        repository=repo, batch_id=batch.batch_id, sheets_gateway=gateway
    )
    assert retry.status == "failed"
    assert "reconcile" in (retry.error or "")
    assert gateway.append_calls == 1


def test_sheet_internal_blank_row_rejected_before_append(repo):
    job = posting(1)
    seed(repo, [job])
    destination = sheet_destination("example123", "Sheet1")
    batch = prepare(repo, request(repo, destination, [job]))
    gateway = FakeSheets()
    gateway.values.extend([[], ["existing", "company", "https://example.com/existing"]])
    with pytest.raises(BatchConflict, match="blank row"):
        batches.finalize_daily_batch(
            repository=repo, batch_id=batch.batch_id, sheets_gateway=gateway
        )
    assert DailyBatchStore(repo).get(batch.batch_id).status == "prepared"
    assert gateway.append_calls == 0

    gateway.values.pop(1)
    delivered = batches.finalize_daily_batch(
        repository=repo, batch_id=batch.batch_id, sheets_gateway=gateway
    )
    assert delivered.status == "delivered"
    assert gateway.append_calls == 1


@pytest.mark.parametrize("quota", [0, -1, True, 1.5, "3"])
def test_invalid_quota(repo, tmp_path, quota):
    with pytest.raises(ValidationError):
        request(repo, tmp_path / "out.csv", [], quota)


def test_decisions_counts_and_partial_provenance(repo, tmp_path):
    jobs = [posting(n) for n in range(4)]
    seed(repo, jobs, ["strong_match", "possible_match", "needs_review", "reject"])
    result = prepare(
        repo,
        request(
            repo,
            tmp_path / "out.csv",
            jobs,
            completeness="partial",
            source_failures=("source timeout",),
        ),
    )
    assert result.selected_count == 2
    assert result.shortfall == 3
    assert result.counts.candidate_postings == 4
    assert result.counts.match_eligible_postings == 2
    assert result.counts.needs_review_postings == result.counts.rejected_postings == 1
    assert DailyBatchStore(repo).get(result.batch_id).request.source_failures == ("source timeout",)
    assert result.request.completeness == "partial"


def test_review_batch_can_select_needs_review_without_auto_default(repo, tmp_path):
    jobs = [posting(10), posting(11)]
    seed(repo, jobs, ["strong_match", "needs_review"])

    auto_result = prepare(
        repo,
        request(repo, tmp_path / "auto.csv", jobs, quota=5),
    )
    assert auto_result.selected_count == 1
    assert auto_result.counts.match_eligible_postings == 1
    assert auto_result.counts.needs_review_postings == 1
    assert auto_result.counts.selection_eligible_postings == 1

    review_result = prepare(
        repo,
        request(
            repo,
            tmp_path / "review.csv",
            jobs,
            quota=5,
            idempotency_key="day-2",
            include_needs_review=True,
        ),
    )
    assert review_result.selected_count == 2
    assert review_result.counts.match_eligible_postings == 1
    assert review_result.counts.needs_review_postings == 1
    assert review_result.counts.selection_eligible_postings == 2
    assert review_result.counts.selection_eligible_groups == 2

    delivered = finalize(repo, review_result)
    assert delivered.status == "delivered"
    assert len(rows(tmp_path / "review.csv")) == 2


@pytest.mark.parametrize("status", ["applied", "not_applied", "unknown"])
def test_historical_group_suppression_precedes_prior(repo, tmp_path, status):
    jobs = [posting(1), posting(2, canonical_url="https://example.com/jobs/1"), posting(3)]
    seed(repo, jobs)
    history(repo, jobs[0], status)
    repo.mark_exported(jobs[1].id, CLIENT, str(tmp_path / "out.csv"))
    # Historical member need not be in the requested candidate scope.
    result = prepare(repo, request(repo, tmp_path / "out.csv", jobs[1:]))
    assert result.selected_count == 1
    assert result.counts.historically_suppressed_groups == 1
    assert result.counts.previously_delivered_groups == 0


def test_history_client_scope_and_url_fallback(repo, tmp_path):
    job = posting(1)
    seed(repo, [job])
    history(repo, job, client="someone-else")
    assert prepare(repo, request(repo, tmp_path / "one.csv", [job])).selected_count == 1
    legacy = job.model_copy(update={"source": "other", "source_job_id": "different"})
    history(repo, legacy)
    assert prepare(repo, request(repo, tmp_path / "two.csv", [job])).selected_count == 0


def test_prior_delivery_destination_and_v2_identity(repo, tmp_path):
    job = posting(1)
    seed(repo, [job])
    path = tmp_path / "out.csv"
    repo.mark_exported(job.id, CLIENT, str(path))
    req = request(repo, path, [job], brief_revision_id="sourcing-v2", brief_sha256="b" * 64)
    result = prepare(repo, req)
    assert result.selected_count == 0
    assert result.counts.previously_delivered_groups == 1
    assert result.request.client_id == CLIENT
    assert prepare(repo, request(repo, tmp_path / "other.csv", [job])).selected_count == 1


def test_group_collapse_representative_and_url(repo, tmp_path):
    jobs = [
        posting(1),
        posting(
            2, canonical_url="https://example.com/jobs/1", apply_url="https://example.com/apply/2"
        ),
        posting(3, canonical_url="https://example.com/jobs/1"),
    ]
    seed(repo, jobs)
    result = prepare(repo, request(repo, tmp_path / "out.csv", jobs))
    assert result.selected_count == 1
    assert result.items[0].representative_job_id == jobs[1].id
    assert result.counts.match_eligible_postings == 3
    assert result.counts.match_eligible_groups == 1
    assert result.counts.duplicate_postings_collapsed == 2
    assert finalize(repo, result).status == "delivered"
    assert rows(tmp_path / "out.csv")[0]["Job Link"] == str(jobs[1].apply_url)


def test_canonical_fallback_and_successful_replay(repo, tmp_path):
    jobs = [posting(1), posting(2)]
    seed(repo, jobs)
    req = request(repo, tmp_path / "out.csv", jobs, 1)
    result = finalize(repo, prepare(repo, req))
    before = (tmp_path / "out.csv").read_bytes()
    assert prepare(repo, req) == result
    assert finalize(repo, result) == result
    assert (tmp_path / "out.csv").read_bytes() == before
    assert rows(tmp_path / "out.csv")[0]["Job Link"] in {str(j.canonical_url) for j in jobs}
    assert (
        prepare(
            repo, request(repo, tmp_path / "out.csv", jobs, idempotency_key="day-2")
        ).selected_count
        == 1
    )


def test_input_and_ingestion_order(tmp_path):
    jobs = [
        posting(1),
        posting(2),
        posting(
            3, canonical_url="https://example.com/jobs/1", apply_url="https://example.com/apply/3"
        ),
    ]
    outputs = []
    for n, values in enumerate((jobs, list(reversed(jobs)))):
        repo = SQLiteRepository(tmp_path / f"{n}.db")
        seed(repo, values)
        result = prepare(repo, request(repo, tmp_path / f"{n}.csv", values))
        outputs.append([(i.delivery_group_id, i.representative_job_id) for i in result.items])
    assert outputs[0] == outputs[1] == sorted(outputs[0])


@pytest.mark.parametrize(
    "changes",
    [
        {"requested_quota": 2},
        {"evaluation_id": "different"},
        {"evidence_sha256": "f" * 64},
        {"brief_revision_id": "v2", "brief_sha256": "b" * 64},
    ],
)
def test_incompatible_key(repo, tmp_path, changes):
    seed(repo, [posting(1)])
    req = request(repo, tmp_path / "out.csv", [posting(1)])
    prepare(repo, req)
    with pytest.raises(BatchConflict, match="incompatible"):
        prepare(repo, DailyBatchRequest.model_validate({**req.model_dump(), **changes}))


def test_evidence_changes_fail_new_scope_but_replay_is_frozen(repo, tmp_path):
    jobs = [posting(1)]
    seed(repo, jobs)
    req = request(repo, tmp_path / "out.csv", jobs)
    result = prepare(repo, req)
    seed(repo, jobs, ["reject"])
    assert prepare(repo, req) == result
    assert finalize(repo, result).status == "failed"
    assert not (tmp_path / "out.csv").exists()
    with pytest.raises(BatchConflict, match="evidence changed"):
        prepare(repo, req.model_copy(update={"idempotency_key": "new"}))


@pytest.mark.parametrize("after_write", [False, True])
def test_write_failure_retry(repo, tmp_path, monkeypatch, after_write):
    jobs = [posting(1), posting(2)]
    seed(repo, jobs)
    result = prepare(repo, request(repo, tmp_path / "out.csv", jobs, 1))
    real = batches.publish_csv

    def fail(*args):
        if after_write:
            real(*args)
        raise OSError("injected export failure")

    monkeypatch.setattr(batches, "publish_csv", fail)
    failed = finalize(repo, result)
    assert failed.status == "failed"
    assert not repo.is_exported(
        result.items[0].representative_job_id, CLIENT, str(tmp_path / "out.csv")
    )
    monkeypatch.setattr(batches, "publish_csv", real)
    delivered = finalize(repo, failed)
    assert delivered.status == "delivered"
    assert delivered.items == result.items
    assert len(rows(tmp_path / "out.csv")) == 1


def test_database_failure_after_file_write_reconciles(repo, tmp_path):
    jobs = [posting(1)]
    seed(repo, jobs)
    result = prepare(repo, request(repo, tmp_path / "out.csv", jobs))
    with repo.connect() as c:
        c.execute(
            "CREATE TRIGGER fail_export BEFORE INSERT ON exports BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    assert finalize(repo, result).status == "failed"
    assert len(rows(tmp_path / "out.csv")) == 1
    with repo.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM group_deliveries").fetchone()[0] == 0
        c.execute("DROP TRIGGER fail_export")
    assert finalize(repo, result).status == "delivered"
    assert len(rows(tmp_path / "out.csv")) == 1


def test_pending_journal_and_external_drift(repo, tmp_path, monkeypatch):
    jobs = [posting(1)]
    seed(repo, jobs)
    one = prepare(repo, request(repo, tmp_path / "out.csv", jobs))
    two = prepare(repo, request(repo, tmp_path / "out.csv", jobs, idempotency_key="two"))
    monkeypatch.setattr(
        batches, "publish_csv", lambda *args: (_ for _ in ()).throw(OSError("fail"))
    )
    assert finalize(repo, one).status == "failed"
    assert "unresolved" in finalize(repo, two).error
    (tmp_path / "out.csv").write_text("external content")
    assert "reconcile" in finalize(repo, one).error
    assert (tmp_path / "out.csv").read_text() == "external content"


def test_competing_prepared_batch_cannot_redeliver(repo, tmp_path):
    jobs = [posting(1)]
    seed(repo, jobs)
    one = prepare(repo, request(repo, tmp_path / "out.csv", jobs))
    two = prepare(repo, request(repo, tmp_path / "out.csv", jobs, idempotency_key="two"))
    assert finalize(repo, one).status == "delivered"
    assert finalize(repo, two).status == "failed"
    assert len(rows(tmp_path / "out.csv")) == 1


def test_blacklist_not_enforced(repo, tmp_path):
    jobs = [posting(1)]
    seed(repo, jobs)
    repo.import_historical_records(
        client_id=CLIENT,
        workbook_sha256="a" * 64,
        records=[],
        blacklist_evidence=[HistoricalBlacklistEvidence("Acme", "company", "Blacklist", 2)],
    )
    assert prepare(repo, request(repo, tmp_path / "out.csv", jobs)).selected_count == 1


def test_legacy_database_preserves_all_existing_tables(repo, tmp_path):
    jobs = [posting(1)]
    seed(repo, jobs)
    history(repo, jobs[0])
    repo.mark_exported(jobs[0].id, CLIENT, "legacy.csv")
    DailyBatchStore(repo)
    with repo.connect() as c:
        for table in ("daily_batch_candidates", "daily_batch_items", "daily_batches"):
            c.execute(f"DROP TABLE {table}")
        names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        before = {name: [tuple(r) for r in c.execute(f"SELECT * FROM {name}")] for name in names}
    reopened = SQLiteRepository(tmp_path / "jobs.db")
    DailyBatchStore(reopened)
    with reopened.connect() as c:
        after = {name: [tuple(r) for r in c.execute(f"SELECT * FROM {name}")] for name in names}
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    assert before == after
    assert prepare(reopened, request(reopened, tmp_path / "out.csv", jobs)).selected_count == 0


def test_generation_column_migration_is_safe_for_concurrent_initializers(repo):
    DailyBatchStore(repo)
    with repo.connect() as connection:
        connection.execute("ALTER TABLE daily_batches DROP COLUMN generation_id")

    with ThreadPoolExecutor(max_workers=2) as pool:
        stores = list(pool.map(lambda _: DailyBatchStore(repo), range(2)))

    assert len(stores) == 2
    with repo.connect() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(daily_batches)").fetchall()
        }
    assert "generation_id" in columns


def test_recreated_batch_id_cannot_finalize_older_generation(repo, tmp_path):
    jobs = [posting(1)]
    seed(repo, jobs)
    req = request(repo, tmp_path / "out.csv", jobs)
    first = prepare(repo, req)
    store = DailyBatchStore(repo)
    store.discard_prepared(
        first.batch_id, expected_generation_id=first.generation_id
    )
    replacement = prepare(repo, req)

    assert replacement.batch_id == first.batch_id
    assert replacement.generation_id != first.generation_id
    with pytest.raises(BatchConflict, match="batch revision changed"):
        batches.finalize_daily_batch(
            repository=repo,
            batch_id=first.batch_id,
            expected_generation_id=first.generation_id,
        )
    assert DailyBatchStore(repo).get(replacement.batch_id).status == "prepared"



def test_recreated_batch_id_cannot_be_discarded_by_older_generation(repo, tmp_path):
    jobs = [posting(1)]
    seed(repo, jobs)
    req = request(repo, tmp_path / "out.csv", jobs)
    first = prepare(repo, req)
    store = DailyBatchStore(repo)
    store.discard_prepared(
        first.batch_id, expected_generation_id=first.generation_id
    )
    replacement = prepare(repo, req)

    assert replacement.batch_id == first.batch_id
    assert replacement.generation_id != first.generation_id
    with pytest.raises(BatchConflict, match="batch revision changed"):
        store.discard_prepared(
            first.batch_id, expected_generation_id=first.generation_id
        )

    current = store.get(replacement.batch_id)
    assert current.generation_id == replacement.generation_id
    assert current.status == "prepared"

def test_process_interruption_after_replace_recovers_on_reopen(repo, tmp_path, monkeypatch):
    class Interrupted(BaseException):
        pass

    jobs = [posting(1)]
    seed(repo, jobs)
    result = prepare(repo, request(repo, tmp_path / "out.csv", jobs))
    real = batches.publish_csv

    def interrupted(*args):
        real(*args)
        raise Interrupted()

    monkeypatch.setattr(batches, "publish_csv", interrupted)
    with pytest.raises(Interrupted):
        finalize(repo, result)
    reopened = SQLiteRepository(tmp_path / "jobs.db")
    assert DailyBatchStore(reopened).get(result.batch_id).status == "prepared"
    monkeypatch.setattr(batches, "publish_csv", real)
    assert finalize(reopened, result).status == "delivered"
    assert len(rows(tmp_path / "out.csv")) == 1


def test_seen_posting_is_delivery_fresh(repo, tmp_path):
    jobs = [posting(1)]
    seed(repo, jobs)
    repo.upsert_job(jobs[0])
    result = prepare(repo, request(repo, tmp_path / "out.csv", jobs))
    assert result.selected_count == 1


def test_group_merge_preserves_batch_history(repo, tmp_path):
    jobs = [posting(1), posting(2)]
    seed(repo, jobs)
    result = finalize(repo, prepare(repo, request(repo, tmp_path / "out.csv", jobs)))
    repo.upsert_job(posting(2, canonical_url=str(jobs[0].canonical_url)))
    assert DailyBatchStore(repo).get(result.batch_id) == result
    with repo.connect() as c:
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_missing_match_and_malformed_destination_fail_closed(repo, tmp_path):
    job = posting(1)
    repo.upsert_job(job)
    with pytest.raises(BatchConflict, match="missing authoritative"):
        request(repo, tmp_path / "out.csv", [job])
    seed(repo, [job])
    (tmp_path / "out.csv").write_text("wrong,header\n")
    result = prepare(repo, request(repo, tmp_path / "out.csv", [job]))
    assert finalize(repo, result).status == "failed"
    assert (tmp_path / "out.csv").read_text() == "wrong,header\n"



class _EmptyBatchCursor:
    def fetchall(self):
        return []


class _RecordingBatchConnection:
    def __init__(self):
        self.calls = []

    def execute(self, statement, parameters=()):
        self.calls.append((statement, tuple(parameters)))
        return _EmptyBatchCursor()


def test_delivery_lookup_helpers_bound_20k_candidate_scope():
    jobs = tuple(f"job-{index}" for index in range(20_000))
    groups = tuple(f"group-{index}" for index in range(20_000))

    group_connection = _RecordingBatchConnection()
    DailyBatchStore._groups_for_jobs(group_connection, jobs)
    assert len(group_connection.calls) == 40
    assert max(len(parameters) for _, parameters in group_connection.calls) <= 500

    delivery_connection = _RecordingBatchConnection()
    DailyBatchStore._delivered_groups(
        delivery_connection,
        groups,
        CLIENT,
        "client-sheet:jobs",
    )
    assert len(delivery_connection.calls) == 40
    assert max(len(parameters) for _, parameters in delivery_connection.calls) <= 502

    history_connection = _RecordingBatchConnection()
    DailyBatchStore._historical_groups(
        history_connection,
        groups,
        CLIENT,
        "client-sheet:jobs",
    )
    assert len(history_connection.calls) == 40
    assert max(len(parameters) for _, parameters in history_connection.calls) <= 503
