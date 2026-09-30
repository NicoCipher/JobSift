from __future__ import annotations

from datetime import UTC, datetime

import pytest

from job_scout.delivery_destinations import (
    ClientSheetDestinationStore,
    logical_destination,
    parse_logical_destination,
    spreadsheet_id_from_value,
)
from job_scout.domain.daily_batch import BatchConflict, DailyBatchRequest
from job_scout.domain.models import Job, JobMatch
from job_scout.normalization.core import content_fingerprint
from job_scout.orchestration.daily_batch import finalize_daily_batch, prepare_daily_batch
from job_scout.storage.daily_batches import DailyBatchStore
from job_scout.storage.sqlite import SQLiteRepository

CLIENT = "client-a"
NOW = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)


class FakeClientSheet:
    def __init__(self):
        self.values = [["Role", "Company", "URL", "Notes", "Stage", "Batch", "Employer"]]
        self.sheet_id = 321
        self.title = "Jobs"
        self.append_calls = 0
        self.uncertain = False

    def sheet_metadata(self, spreadsheet_id):
        assert spreadsheet_id == "sheet123"
        return [{"sheet_id": self.sheet_id, "title": self.title}]

    def read_rows(self, spreadsheet_id, tab):
        assert (spreadsheet_id, tab) == ("sheet123", "Jobs")
        return [row.copy() for row in self.values]

    def read_table_rows(self, spreadsheet_id, tab):
        return self.read_rows(spreadsheet_id, tab)

    def append_table_rows(self, spreadsheet_id, tab, rows):
        assert (spreadsheet_id, tab) == ("sheet123", "Jobs")
        self.append_calls += 1
        self.values.extend([row.copy() for row in rows])
        if self.uncertain:
            self.uncertain = False
            raise OSError("response lost after commit")

    # Legacy methods are unused by mapped destinations but satisfy the runtime protocol.
    def append_rows(self, spreadsheet_id, tab, rows):
        self.append_table_rows(spreadsheet_id, tab, rows)


def job() -> Job:
    description = "Build backend services."
    return Job(
        id="job-1",
        source="greenhouse",
        source_job_id="1",
        source_board_id="acme",
        title="Backend Engineer",
        company="Acme",
        employer_id="acme",
        description_text=description,
        job_url="https://example.com/jobs/1",
        canonical_url="https://example.com/jobs/1",
        posted_at=NOW,
        discovered_at=NOW,
        last_seen_at=NOW,
        content_fingerprint=content_fingerprint(
            title="Backend Engineer",
            description=description,
            location=None,
            employment_type=None,
        ),
    )


def seed(repo: SQLiteRepository, posting: Job) -> None:
    repo.upsert_job(posting)
    repo.save_match(
        JobMatch(
            job_id=posting.id,
            client_id=CLIENT,
            decision="strong_match",
            evaluated_at=NOW,
            matcher_version="test",
        )
    )


def register(repo, gateway, mapping=None):
    return ClientSheetDestinationStore(repo).register_google_sheet(
        client_id=CLIENT,
        destination_id="primary-jobs",
        display_name="Client Jobs",
        spreadsheet="https://docs.google.com/spreadsheets/d/sheet123/edit",
        tab_name="Jobs",
        column_mapping=mapping
        or {
            "Job Title": "Role",
            "Company Name": "Company",
            "Job Link": "URL",
            "Status": "Stage",
            "Batch ID": "Batch",
        },
        gateway=gateway,
    )


def prepared(repo, destination):
    posting = job()
    seed(repo, posting)
    request = DailyBatchRequest(
        client_id=CLIENT,
        destination=destination.logical_uri,
        destination_id=destination.destination_id,
        destination_config_sha256=destination.config_sha256,
        idempotency_key="day-1",
        requested_quota=1,
        evidence_scope_id="scope",
        evaluation_id="eval",
        candidate_job_ids=(posting.id,),
        evidence_sha256=DailyBatchStore(repo).evidence_digest(CLIENT, (posting.id,)),
    )
    return prepare_daily_batch(repository=repo, request=request)


def test_google_sheet_url_and_logical_destination_are_canonical():
    assert (
        spreadsheet_id_from_value("https://docs.google.com/spreadsheets/d/sheet123/edit#gid=321")
        == "sheet123"
    )
    value = logical_destination("primary-jobs")
    assert value == "client-sheet://primary-jobs"
    assert parse_logical_destination(value) == "primary-jobs"


def test_registration_persists_stable_sheet_identity_and_mapping(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    gateway = FakeClientSheet()
    destination = register(repo, gateway)

    assert destination.sheet_id == 321
    assert destination.tab_name == "Jobs"
    assert destination.column_mapping["Job Link"] == "URL"
    assert destination.logical_uri == "client-sheet://primary-jobs"
    assert ClientSheetDestinationStore(repo).get(CLIENT, "primary-jobs") == destination


def test_physical_worksheet_cannot_be_registered_to_two_clients(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    gateway = FakeClientSheet()
    first = register(repo, gateway)
    assert first.client_id == CLIENT

    with pytest.raises(BatchConflict, match="already registered"):
        ClientSheetDestinationStore(repo).register_google_sheet(
            client_id="client-b",
            destination_id="client-b-jobs",
            display_name="Client B Jobs",
            spreadsheet="https://docs.google.com/spreadsheets/d/sheet123/edit",
            tab_name="Jobs",
            column_mapping={"Job Link": "URL"},
            gateway=gateway,
        )

    assert ClientSheetDestinationStore(repo).get(CLIENT, "primary-jobs") == first


def test_same_client_cannot_alias_one_worksheet_as_two_destinations(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    gateway = FakeClientSheet()
    register(repo, gateway)

    with pytest.raises(BatchConflict, match="already registered"):
        ClientSheetDestinationStore(repo).register_google_sheet(
            client_id=CLIENT,
            destination_id="secondary-jobs",
            display_name="Duplicate Alias",
            spreadsheet="sheet123",
            tab_name="Jobs",
            column_mapping={"Job Link": "URL"},
            gateway=gateway,
        )


def test_registration_requires_unique_existing_mapped_headers(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    gateway = FakeClientSheet()
    with pytest.raises(ValueError, match="Job Link"):
        register(repo, gateway, {"Job Title": "Role"})
    gateway.values[0].append("URL")
    with pytest.raises(ValueError, match="exactly once"):
        register(repo, gateway, {"Job Link": "URL"})


def test_client_owned_sheet_recovers_uncertain_append_while_client_edits_unowned_cells(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    gateway = FakeClientSheet()
    destination = register(repo, gateway)
    batch = prepared(repo, destination)

    gateway.uncertain = True
    failed = finalize_daily_batch(
        repository=repo, batch_id=batch.batch_id, sheets_gateway=gateway
    )
    assert failed.status == "failed"
    assert gateway.append_calls == 1
    assert len(gateway.values) == 2

    # The client may update their Notes or mapped Status without breaking recovery.
    gateway.values[1][3] = "reviewed by client"
    gateway.values[1][4] = "Applied"

    recovered = finalize_daily_batch(
        repository=repo, batch_id=batch.batch_id, sheets_gateway=gateway
    )
    assert recovered.status == "delivered"
    assert gateway.append_calls == 1
    assert gateway.values[1][0:3] == ["Backend Engineer", "Acme", "https://example.com/jobs/1"]
    assert gateway.values[1][3] == "reviewed by client"
    assert gateway.values[1][4] == "Applied"
    assert gateway.values[1][5] == batch.batch_id


def test_header_or_tab_identity_drift_fails_closed(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    gateway = FakeClientSheet()
    destination = register(repo, gateway)
    batch = prepared(repo, destination)

    gateway.values[0][0] = "Position"
    failed = finalize_daily_batch(
        repository=repo, batch_id=batch.batch_id, sheets_gateway=gateway
    )
    assert failed.status == "failed"
    assert gateway.append_calls == 0

    # A fresh batch proves the stable Google sheetId catches tab renames too.
    repo2 = SQLiteRepository(tmp_path / "jobs2.db")
    gateway2 = FakeClientSheet()
    destination2 = register(repo2, gateway2)
    batch2 = prepared(repo2, destination2)
    gateway2.title = "Renamed Jobs"
    failed2 = finalize_daily_batch(
        repository=repo2, batch_id=batch2.batch_id, sheets_gateway=gateway2
    )
    assert failed2.status == "failed"
    assert gateway2.append_calls == 0


def test_destination_reconfiguration_after_prepare_cannot_change_frozen_delivery(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    gateway = FakeClientSheet()
    original = register(repo, gateway)
    batch = prepared(repo, original)

    changed = register(
        repo,
        gateway,
        {
            "Job Title": "Role",
            "Company Name": "Employer",
            "Job Link": "URL",
        },
    )
    assert changed.config_sha256 != original.config_sha256

    failed = finalize_daily_batch(
        repository=repo, batch_id=batch.batch_id, sheets_gateway=gateway
    )
    assert failed.status == "failed"
    assert "changed after batch preparation" in (failed.error or "")
    assert gateway.append_calls == 0


def test_registration_carries_legacy_sheet_delivery_history_into_logical_destination(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    gateway = FakeClientSheet()
    posting = job()
    seed(repo, posting)

    legacy_destination = "gsheet://sheet123/Jobs"
    repo.mark_exported(posting.id, CLIENT, legacy_destination)

    destination = register(repo, gateway)
    with repo.connect() as connection:
        group = connection.execute(
            "SELECT group_id FROM posting_delivery_groups WHERE job_id=?",
            (posting.id,),
        ).fetchone()[0]
        assert connection.execute(
            "SELECT 1 FROM group_deliveries "
            "WHERE group_id=? AND client_id=? AND destination=?",
            (group, CLIENT, destination.logical_uri),
        ).fetchone() is not None
        assert connection.execute(
            "SELECT 1 FROM exports "
            "WHERE job_id=? AND client_id=? AND destination=?",
            (posting.id, CLIENT, destination.logical_uri),
        ).fetchone() is not None

    request = DailyBatchRequest(
        client_id=CLIENT,
        destination=destination.logical_uri,
        destination_id=destination.destination_id,
        destination_config_sha256=destination.config_sha256,
        idempotency_key="day-2",
        requested_quota=1,
        evidence_scope_id="scope-2",
        evaluation_id="eval-2",
        candidate_job_ids=(posting.id,),
        evidence_sha256=DailyBatchStore(repo).evidence_digest(CLIENT, (posting.id,)),
    )
    result = prepare_daily_batch(repository=repo, request=request)

    assert result.selected_count == 0
    assert result.counts.previously_delivered_groups == 1


def test_logical_destination_keeps_duplicate_history_stable_when_physical_mapping_changes(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    gateway = FakeClientSheet()
    first_destination = register(repo, gateway)
    first = prepared(repo, first_destination)
    assert (
        finalize_daily_batch(repository=repo, batch_id=first.batch_id, sheets_gateway=gateway).status
        == "delivered"
    )

    # Re-registering physical details keeps the logical destination ID unchanged.
    second_destination = register(
        repo,
        gateway,
        {
            "Job Title": "Role",
            "Company Name": "Employer",
            "Job Link": "URL",
        },
    )
    posting = job()
    request = DailyBatchRequest(
        client_id=CLIENT,
        destination=second_destination.logical_uri,
        destination_id=second_destination.destination_id,
        destination_config_sha256=second_destination.config_sha256,
        idempotency_key="day-2",
        requested_quota=1,
        evidence_scope_id="scope-2",
        evaluation_id="eval-2",
        candidate_job_ids=(posting.id,),
        evidence_sha256=DailyBatchStore(repo).evidence_digest(CLIENT, (posting.id,)),
    )
    result = prepare_daily_batch(repository=repo, request=request)

    assert result.selected_count == 0
    assert result.counts.previously_delivered_groups == 1
