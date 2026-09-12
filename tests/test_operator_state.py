import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from test_daily_batch import CLIENT, finalize, history, posting, prepare, request, seed

from job_scout.domain.operator_state import (
    OperatorOutcomeCommand,
    OperatorOutcomeImportRecord,
    OperatorOutcomeSubject,
    OutcomeConflict,
)
from job_scout.storage.operator_state import OperatorStateStore
from job_scout.storage.sqlite import SQLiteRepository


def subject(id="job-1", type="posting"):
    return OperatorOutcomeSubject(type=type, id=id)


def command(**changes):
    values = {
        "client_id": CLIENT,
        "subject": subject(),
        "value": "applied",
        "expected_version": 0,
        "actor_type": "operator",
        "actor_id": "alice",
        "idempotency_key": "command-key-0001",
        "source_reference": "trusted-domain-caller",
    }
    values.update(changes)
    return OperatorOutcomeCommand(**values)


def imported(**changes):
    values = {
        "client_id": CLIENT,
        "external_record_id": "row-1",
        "subject_type": "posting",
        "source": "greenhouse",
        "source_board_id": "acme",
        "source_job_id": "1",
        "observed_outcome": "applied",
        "expected_version": 0,
        "actor_type": "importer",
        "actor_id": "import-service",
        "source_reference": "outcomes-file-sha256",
    }
    values.update(changes)
    return OperatorOutcomeImportRecord(**values)


@pytest.fixture
def repo(tmp_path):
    repo = SQLiteRepository(tmp_path / "state.db")
    seed(repo, [posting(1), posting(2, canonical_url="https://example.com/jobs/1")])
    return repo


@pytest.fixture
def store(repo):
    return OperatorStateStore(repo)


def delivered(repo, client=CLIENT):
    repo.mark_exported("job-1", client, "fixture.csv")


def snapshot(repo):
    with repo.connect() as c:
        names = [
            r[0]
            for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'operator_outcome_%'"
            )
        ]
        return {name: [tuple(r) for r in c.execute(f"SELECT * FROM {name}")] for name in names}


def history_subject(repo, value="unknown", client=CLIENT):
    history(repo, posting(90), value, client)
    with repo.connect() as c:
        id = c.execute(
            "SELECT id FROM historical_job_links WHERE client_id=?", (client,)
        ).fetchone()[0]
    return subject(str(id), "history_entry")


def test_initial_unknown_does_not_establish_freshness(store):
    p = store.get_projection(CLIENT, subject())
    assert (p.value, p.version, p.current_source) == ("unknown", 0, "unknown")
    assert store.get_surfacing_evidence(CLIENT, subject()) == ()


@pytest.mark.parametrize("value", ["applied", "not_applied"])
def test_delivered_outcome_preserves_match_and_delivery(repo, store, value):
    delivered(repo)
    before = snapshot(repo)
    lower = datetime.now(UTC)
    result = store.record_outcome(command(value=value))
    assert lower <= result.event.recorded_at <= datetime.now(UTC)
    assert result.projection.value == value
    assert result.event.actor_id == "alice"
    assert result.event.previous_value == "unknown"
    assert result.event.previous_version == 0
    assert snapshot(repo) == before
    assert len(store.get_surfacing_evidence(CLIENT, subject())) == 2


@pytest.mark.parametrize("changes", [{}, {"client_id": "other"}, {"subject": subject("job-2")}])
def test_unsurfaced_wrong_client_and_alternate_member_fail(repo, store, changes):
    if changes:
        delivered(repo)
    with pytest.raises(OutcomeConflict, match="not surfaced"):
        store.record_outcome(command(**changes))
    assert store.list_events(CLIENT, subject()) == ()


@pytest.mark.parametrize(
    "value", ["unknown", "reset", "relevant", "not_relevant", "reviewed", "dismissed"]
)
def test_unsupported_writable_values(value):
    with pytest.raises(ValidationError):
        command(value=value)


def test_no_group_subject_or_caller_timestamp():
    with pytest.raises(ValidationError):
        subject(type="group")
    with pytest.raises(ValidationError):
        command(recorded_at=datetime.now(UTC))
    with pytest.raises(ValidationError):
        command(expected_version=True)


@pytest.mark.parametrize("baseline", ["applied", "not_applied", "unknown"])
def test_historical_only_baseline_and_override(repo, store, baseline):
    s = history_subject(repo, baseline)
    before = snapshot(repo)
    p = store.get_projection(CLIENT, s)
    assert (p.value, p.baseline_value, p.version) == (baseline, baseline, 0)
    assert p.current_source == "historical_import"
    assert p.baseline_import_id
    result = store.record_outcome(command(subject=s))
    assert result.projection.value == "applied"
    assert result.projection.baseline_value == baseline
    assert result.projection.current_source == "explicit_event"
    assert snapshot(repo) == before
    assert all(row[0] != "job-90" for row in before["jobs"])


def test_history_wrong_client_and_missing_subject(store, repo):
    s = history_subject(repo)
    with pytest.raises(OutcomeConflict):
        store.record_outcome(command(client_id="other", subject=s))
    with pytest.raises(OutcomeConflict):
        store.get_projection(CLIENT, subject("99999", "history_entry"))


def test_append_versions_replay_and_stale_conflict(repo, store):
    delivered(repo)
    first = store.record_outcome(command())
    second = store.record_outcome(
        command(value="not_applied", expected_version=1, idempotency_key="command-key-0002")
    )
    assert [e.version for e in store.list_events(CLIENT, subject())] == [1, 2]
    assert store.list_events(CLIENT, subject()) == (first.event, second.event)
    assert store.record_outcome(command()) == first
    assert store.get_projection(CLIENT, subject()).value == "not_applied"
    with pytest.raises(OutcomeConflict, match="stale"):
        store.record_outcome(command(idempotency_key="command-key-0003"))
    with pytest.raises(OutcomeConflict, match="incompatible"):
        store.record_outcome(command(value="not_applied"))


def test_actor_and_client_idempotency_scope(repo, store):
    delivered(repo)
    delivered(repo, "other")
    one = store.record_outcome(command())
    two = store.record_outcome(command(actor_id="bob", expected_version=1))
    other = store.record_outcome(command(client_id="other"))
    assert (one.event.version, two.event.version, other.event.version) == (1, 2, 1)
    assert len(store.list_events("other", subject())) == 1
    assert store.get_projection("other", subject()).latest_event_id == other.event.event_id


def test_daily_batch_integration_and_v2_client(repo, store, tmp_path):
    jobs = [posting(1), posting(2, canonical_url="https://example.com/jobs/1")]
    batch = finalize(
        repo,
        prepare(
            repo,
            request(
                repo,
                tmp_path / "out.csv",
                jobs,
                brief_revision_id="sourcing-v2",
                brief_sha256="b" * 64,
            ),
        ),
    )
    s = subject(batch.items[0].representative_job_id)
    before = snapshot(repo)
    assert store.record_outcome(command(subject=s)).projection.value == "applied"
    assert snapshot(repo) == before
    assert finalize(repo, batch) == batch


@pytest.mark.parametrize("resolution", ["identity", "url", "parsed_url"])
def test_import_resolution_replay(repo, store, resolution):
    delivered(repo)
    if resolution == "identity":
        record = imported(vacancy_url="https://example.com/different")
    elif resolution == "url":
        # Only one exact URL target; the other group member is not automatically selected.
        repo.upsert_job(posting(2, canonical_url="https://example.com/jobs/2"))
        record = imported(
            source=None,
            source_board_id=None,
            source_job_id=None,
            vacancy_url="https://example.com/jobs/1/?utm_source=external#fragment",
        )
    else:
        record = imported(
            source=None,
            source_board_id=None,
            source_job_id=None,
            vacancy_url="https://job-boards.greenhouse.io/acme/jobs/1",
        )
    result = store.import_outcome(record)
    assert result.projection.subject == subject()
    assert result.event.action == "import_outcome"
    assert store.import_outcome(record) == result
    assert len(store.list_events(CLIENT, subject())) == 1
    with pytest.raises(OutcomeConflict, match="incompatible"):
        store.import_outcome(record.model_copy(update={"observed_outcome": "not_applied"}))


def test_unknown_import_retains_evidence_without_reset(repo, store):
    delivered(repo)
    applied = store.record_outcome(command())
    record = imported(observed_outcome="unknown", expected_version=1)
    result = store.import_outcome(record)
    assert result.event is None
    assert result.projection == applied.projection
    store.record_outcome(
        command(value="not_applied", expected_version=1, idempotency_key="command-key-0002")
    )
    assert store.import_outcome(record) == result
    assert store.get_projection(CLIENT, subject()).value == "not_applied"
    with repo.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM operator_outcome_imports").fetchone()[0] == 1
        assert (
            "unknown" in c.execute("SELECT record_json FROM operator_outcome_imports").fetchone()[0]
        )


def test_ambiguous_url_fails_closed(repo, store):
    delivered(repo)
    with pytest.raises(OutcomeConflict, match="ambiguous"):
        store.import_outcome(
            imported(
                source=None,
                source_board_id=None,
                source_job_id=None,
                vacancy_url="https://example.com/jobs/1",
            )
        )


def test_history_identity_ambiguity_does_not_fall_back(repo, store):
    from job_scout.history import HistoricalRecord

    records = [
        HistoricalRecord(
            f"https://example.com/{n}",
            f"https://example.com/{n}",
            "greenhouse",
            "acme",
            "90",
            "Title",
            "Company",
            "unknown",
            "Sheet",
            n,
        )
        for n in (1, 2)
    ]
    repo.import_historical_records(client_id=CLIENT, workbook_sha256="c" * 64, records=records)
    with pytest.raises(OutcomeConflict, match="ambiguous"):
        store.import_outcome(
            imported(
                subject_type="history_entry",
                source_job_id="90",
                vacancy_url="https://example.com/1",
            )
        )


def test_history_import_does_not_transfer_to_posting(repo, store):
    s = history_subject(repo, "not_applied")
    result = store.import_outcome(imported(subject_type="history_entry", source_job_id="90"))
    assert result.projection.subject == s
    assert result.projection.baseline_value == "not_applied"
    assert store.get_projection(CLIENT, subject()).value == "unknown"


def test_no_title_company_resolution():
    with pytest.raises(ValidationError):
        imported(
            source=None,
            source_board_id=None,
            source_job_id=None,
            title="Support Engineer",
            company="Acme",
        )
    with pytest.raises(ValidationError):
        imported(source_board_id=None)


def test_blacklist_does_not_affect_outcome(repo, store):
    from job_scout.history import HistoricalBlacklistEvidence

    delivered(repo)
    repo.import_historical_records(
        client_id=CLIENT,
        workbook_sha256="d" * 64,
        records=[],
        blacklist_evidence=[HistoricalBlacklistEvidence("Acme", "company", "Sheet", 1)],
    )
    assert store.record_outcome(command()).projection.value == "applied"


def test_additive_store_preserves_all_legacy_tables(repo, tmp_path):
    delivered(repo)
    history_subject(repo)
    batch = prepare(repo, request(repo, tmp_path / "batch.csv", [posting(1)]))
    assert batch.status == "prepared"
    before = snapshot(repo)
    OperatorStateStore(repo)
    OperatorStateStore(repo)
    assert snapshot(repo) == before


def test_append_only_database_guards(repo, store):
    delivered(repo)
    store.record_outcome(command())
    store.import_outcome(imported(observed_outcome="unknown", expected_version=1))
    for table in ("operator_outcome_events", "operator_outcome_imports"):
        for sql in (f"DELETE FROM {table}", f"UPDATE {table} SET client_id=client_id"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"), repo.connect() as c:
                c.execute(sql)


def test_concurrent_writers_one_version_wins(repo, store):
    delivered(repo)

    def write(n):
        try:
            return store.record_outcome(
                command(
                    idempotency_key=f"concurrent-key-{n:03}",
                    value="applied" if n == 1 else "not_applied",
                )
            ).event.version
        except OutcomeConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, [1, 2]))
    assert sorted(results, key=str) == [1, "conflict"]
    assert len(store.list_events(CLIENT, subject())) == 1


def test_group_merge_preserves_exact_posting_events(repo, store):
    delivered(repo)
    result = store.record_outcome(command())
    repo.upsert_job(posting(3))
    repo.upsert_job(posting(3, canonical_url="https://example.com/jobs/1"))
    assert store.get_projection(CLIENT, subject()) == result.projection
    assert store.list_events(CLIENT, subject()) == (result.event,)
    with pytest.raises(OutcomeConflict):
        store.record_outcome(command(subject=subject("job-3"), idempotency_key="command-key-0002"))


def test_import_transaction_rolls_back_event_if_receipt_fails(repo, store):
    delivered(repo)
    with repo.connect() as c:
        c.execute(
            "CREATE TRIGGER fail_import BEFORE INSERT ON operator_outcome_imports BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.import_outcome(imported())
    assert store.list_events(CLIENT, subject()) == ()


@pytest.mark.parametrize("retained", ["exports", "group_deliveries"])
def test_either_exact_delivery_table_is_authoritative(repo, store, retained):
    delivered(repo)
    other = "exports" if retained == "group_deliveries" else "group_deliveries"
    with repo.connect() as c:
        c.execute(f"DELETE FROM {other}")
    assert store.record_outcome(command()).projection.value == "applied"


def test_unknown_import_receipt_and_restart_replay(repo, store):
    delivered(repo)
    record = imported(observed_outcome="unknown")
    result = store.import_outcome(record)
    assert result.projection.version == 0
    assert result.projection.value == "unknown"
    assert store.list_events(CLIENT, subject()) == ()
    store.record_outcome(command())
    reopened = OperatorStateStore(repo)
    receipt = reopened.get_import_receipt(
        actor_type=record.actor_type,
        actor_id=record.actor_id,
        client_id=record.client_id,
        source_reference=record.source_reference,
        external_record_id=record.external_record_id,
    )
    assert receipt.record == record
    assert receipt.result == result == reopened.import_outcome(record)
    assert reopened.get_projection(CLIENT, subject()).value == "applied"


def test_unknown_import_cannot_make_unsurfaced_posting_writable(store):
    with pytest.raises(OutcomeConflict, match="not surfaced"):
        store.import_outcome(imported(observed_outcome="unknown"))


def test_outcome_on_history_does_not_grant_posting_eligibility(repo, store):
    history(repo, posting(1), "applied")
    with pytest.raises(OutcomeConflict, match="not surfaced"):
        store.record_outcome(command())


def test_concurrent_identical_replay_returns_one_event(repo, store):
    delivered(repo)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: store.record_outcome(command()), [1, 2]))
    assert results[0] == results[1]
    assert len(store.list_events(CLIENT, subject())) == 1


def test_historical_normalized_url_ambiguity_fails_closed(repo, store):
    from job_scout.history import HistoricalRecord

    records = [
        HistoricalRecord(
            "https://example.com/vacancy",
            "https://example.com/vacancy",
            None,
            None,
            None,
            "Title",
            "Company",
            "unknown",
            "History",
            n,
        )
        for n in (1, 2)
    ]
    repo.import_historical_records(client_id=CLIENT, workbook_sha256="e" * 64, records=records)
    before = snapshot(repo)
    with pytest.raises(OutcomeConflict, match="ambiguous"):
        store.import_outcome(
            imported(
                subject_type="history_entry",
                source=None,
                source_board_id=None,
                source_job_id=None,
                vacancy_url="https://example.com/vacancy/?utm_source=test",
            )
        )
    assert snapshot(repo) == before
    with repo.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM operator_outcome_events").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM operator_outcome_imports").fetchone()[0] == 0
