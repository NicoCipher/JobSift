import csv
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from job_scout.domain.daily_batch import BatchConflict, DailyBatchRequest
from job_scout.domain.models import Job, JobMatch
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
