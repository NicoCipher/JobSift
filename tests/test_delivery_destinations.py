from __future__ import annotations

from datetime import UTC, datetime

import pytest

from job_scout.delivery_destinations import (
    inspect_client_sheet,
    parse_google_sheet_url,
    register_client_sheet,
)
from job_scout.domain.daily_batch import BatchConflict, DailyBatchRequest
from job_scout.domain.delivery import WorksheetMetadata
from job_scout.domain.models import Job, JobMatch
from job_scout.export.batch_sheets import managed_sheet_destination
from job_scout.orchestration.daily_batch import finalize_daily_batch, prepare_daily_batch
from job_scout.storage.daily_batches import DailyBatchStore
from job_scout.storage.destinations import DeliveryDestinationStore
from job_scout.storage.sqlite import SQLiteRepository

NOW = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)


class FakeClientSheet:
    def __init__(self):
        self.metadata = [WorksheetMetadata(worksheet_id=77, title="Jobs", index=0)]
        self.values = [["Role", "Employer", "URL", "Notes", "Status"]]
        self.append_calls = 0

    def worksheets(self, spreadsheet_id):
        assert spreadsheet_id == "clientSheet123"
        return list(self.metadata)

    def read_table(self, spreadsheet_id, tab, width):
        assert (spreadsheet_id, tab, width) == ("clientSheet123", "Jobs", 5)
        return [row.copy() for row in self.values]

    def append_table_rows(self, spreadsheet_id, tab, width, rows):
        assert (spreadsheet_id, tab, width) == ("clientSheet123", "Jobs", 5)
        self.append_calls += 1
        self.values.extend([row.copy() for row in rows])

    # Legacy publisher methods are deliberately unavailable in these tests.
    def read_rows(self, spreadsheet_id, tab):
        raise AssertionError("managed destination must not use legacy read_rows")

    def append_rows(self, spreadsheet_id, tab, rows):
        raise AssertionError("managed destination must not use legacy append_rows")


def job() -> Job:
    return Job(
        id="job-1",
        source="greenhouse",
        source_job_id="1",
        source_board_id="acme",
        title="Backend Engineer",
        company="Acme",
        description_text="Build APIs.",
        job_url="https://example.com/jobs/1",
        canonical_url="https://example.com/jobs/1",
        content_fingerprint="fingerprint",
        discovered_at=NOW,
        last_seen_at=NOW,
    )


def seed(repo: SQLiteRepository, value: Job) -> None:
    repo.upsert_job(value)
    repo.save_match(
        JobMatch(
            job_id=value.id,
            client_id="client-a",
            decision="strong_match",
            evaluated_at=NOW,
            matcher_version="test",
        )
    )


def request(repo, value, contract, *, key="day-1"):
    ids = (value.id,)
    return DailyBatchRequest(
        client_id="client-a",
        destination=managed_sheet_destination(contract.destination_id),
        sheet_delivery=contract,
        idempotency_key=key,
        requested_quota=1,
        evidence_scope_id=key,
        evaluation_id=key,
        candidate_job_ids=ids,
        evidence_sha256=DailyBatchStore(repo).evidence_digest("client-a", ids),
    )


def registered(repo, gateway):
    destination, campaign = register_client_sheet(
        repository=repo,
        gateway=gateway,
        client_id="client-a",
        destination_id="client-a-jobs",
        campaign_id="client-a-software-001",
        sheet_url="https://docs.google.com/spreadsheets/d/clientSheet123/edit#gid=77",
    )
    return destination, campaign, DeliveryDestinationStore(repo).resolve_campaign(
        client_id="client-a",
        campaign_id=campaign.campaign_id,
    )


def test_google_sheet_url_and_arbitrary_header_mapping(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    gateway = FakeClientSheet()

    assert parse_google_sheet_url(
        "https://docs.google.com/spreadsheets/d/clientSheet123/edit#gid=77"
    ) == ("clientSheet123", 77)

    inspected = inspect_client_sheet(
        gateway=gateway,
        sheet_url="https://docs.google.com/spreadsheets/d/clientSheet123/edit#gid=77",
    )
    assert inspected["worksheet_id"] == 77
    assert inspected["column_map"] == {
        "Company Name": 1,
        "Job Link": 2,
        "Job Title": 0,
        "Status": 4,
    }

    destination, campaign, contract = registered(repo, gateway)
    assert destination.client_id == campaign.client_id == contract.client_id == "client-a"
    assert contract.destination_id == "client-a-jobs"
    assert contract.campaign_id == "client-a-software-001"


def test_managed_release_maps_columns_and_sheet_is_not_dedupe_source(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    gateway = FakeClientSheet()
    _, _, contract = registered(repo, gateway)
    value = job()
    seed(repo, value)

    prepared = prepare_daily_batch(repository=repo, request=request(repo, value, contract))
    delivered = finalize_daily_batch(
        repository=repo,
        batch_id=prepared.batch_id,
        sheets_gateway=gateway,
    )

    assert delivered.status == "delivered"
    assert gateway.append_calls == 1
    assert gateway.values[1] == [
        "Backend Engineer",
        "Acme",
        "https://example.com/jobs/1",
        "",
        "",
    ]

    # The client can delete the row. JobSift's delivery ledger still prevents a duplicate.
    gateway.values = [gateway.values[0]]
    next_batch = prepare_daily_batch(
        repository=repo,
        request=request(repo, value, contract, key="day-2"),
    )
    assert next_batch.selected_count == 0
    assert next_batch.counts.previously_delivered_groups == 1


def test_header_change_and_tab_rename_fail_closed(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    gateway = FakeClientSheet()
    _, _, contract = registered(repo, gateway)
    value = job()
    seed(repo, value)

    first = prepare_daily_batch(repository=repo, request=request(repo, value, contract))
    gateway.values[0][2] = "Changed URL Header"
    failed = finalize_daily_batch(
        repository=repo,
        batch_id=first.batch_id,
        sheets_gateway=gateway,
    )
    assert failed.status == "failed"
    assert gateway.append_calls == 0


def test_tab_rename_detected_by_stable_worksheet_id(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    gateway = FakeClientSheet()
    _, _, contract = registered(repo, gateway)
    value = job()
    seed(repo, value)
    prepared = prepare_daily_batch(repository=repo, request=request(repo, value, contract))

    gateway.metadata = [WorksheetMetadata(worksheet_id=77, title="Renamed", index=0)]
    failed = finalize_daily_batch(
        repository=repo,
        batch_id=prepared.batch_id,
        sheets_gateway=gateway,
    )
    assert failed.status == "failed"
    assert "renamed" in (failed.error or "").casefold()
    assert gateway.append_calls == 0


def test_cross_client_and_physical_sheet_reuse_are_blocked(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    gateway = FakeClientSheet()
    registered(repo, gateway)
    store = DeliveryDestinationStore(repo)

    with pytest.raises(BatchConflict, match="not found for client"):
        store.resolve_campaign(client_id="client-b", campaign_id="client-a-software-001")

    inspected = inspect_client_sheet(
        gateway=gateway,
        sheet_url="https://docs.google.com/spreadsheets/d/clientSheet123/edit#gid=77",
    )
    with pytest.raises(BatchConflict, match="already registered"):
        store.register_destination(
            destination_id="client-b-jobs",
            client_id="client-b",
            **inspected,
        )


def test_only_one_active_campaign_can_write_to_destination(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    gateway = FakeClientSheet()
    registered(repo, gateway)
    store = DeliveryDestinationStore(repo)

    with pytest.raises(BatchConflict, match="another active campaign"):
        store.bind_campaign(
            campaign_id="client-a-second",
            client_id="client-a",
            destination_id="client-a-jobs",
        )

    store.set_campaign_status(
        client_id="client-a",
        campaign_id="client-a-software-001",
        status="completed",
    )
    second = store.bind_campaign(
        campaign_id="client-a-second",
        client_id="client-a",
        destination_id="client-a-jobs",
    )
    assert second.status == "active"


def test_multi_tab_sheet_requires_gid_or_explicit_name(tmp_path):
    gateway = FakeClientSheet()
    gateway.metadata.append(WorksheetMetadata(worksheet_id=88, title="Archive", index=1))

    with pytest.raises(BatchConflict, match="multiple worksheets"):
        inspect_client_sheet(
            gateway=gateway,
            sheet_url="https://docs.google.com/spreadsheets/d/clientSheet123/edit",
        )
