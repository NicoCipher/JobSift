from __future__ import annotations

import sys
from datetime import UTC, datetime

import pytest

from job_scout.cli import main
from job_scout.delivery_destinations import ClientSheetDestinationStore
from job_scout.delivery_profiles import (
    ClientDeliveryProfileStore,
    delivery_profile_control_id,
)
from job_scout.domain.daily_batch import BatchConflict, DailyBatchRequest
from job_scout.domain.models import Job, JobMatch
from job_scout.normalization.core import content_fingerprint
from job_scout.orchestration.daily_batch import finalize_daily_batch, prepare_daily_batch
from job_scout.storage.daily_batches import DailyBatchStore
from job_scout.storage.sqlite import SQLiteRepository


class FakeSheet:
    def __init__(self):
        self.values = [["Role", "Company", "URL"]]

    def sheet_metadata(self, spreadsheet_id):
        assert spreadsheet_id == "sheet123"
        return [{"sheet_id": 1, "title": "Jobs"}]

    def read_rows(self, spreadsheet_id, tab):
        assert (spreadsheet_id, tab) == ("sheet123", "Jobs")
        return [row.copy() for row in self.values]

    def read_table_rows(self, spreadsheet_id, tab):
        return self.read_rows(spreadsheet_id, tab)

    def append_table_rows(self, spreadsheet_id, tab, rows):
        assert (spreadsheet_id, tab) == ("sheet123", "Jobs")
        self.values.extend([row.copy() for row in rows])

    def append_rows(self, spreadsheet_id, tab, rows):
        self.append_table_rows(spreadsheet_id, tab, rows)


def make_job(job_id: str, company: str) -> Job:
    now = datetime.now(UTC)
    description = "Build production software."
    return Job(
        id=job_id,
        source="greenhouse",
        source_job_id=job_id,
        source_board_id=company.casefold(),
        title="Software Engineer",
        company=company,
        employer_id=company.casefold(),
        description_text=description,
        job_url=f"https://example.com/{job_id}",
        canonical_url=f"https://example.com/{job_id}",
        posted_at=now,
        discovered_at=now,
        last_seen_at=now,
        content_fingerprint=content_fingerprint(
            title="Software Engineer",
            description=description,
            location=None,
            employment_type=None,
        ),
    )


def setup_profile(
    repo: SQLiteRepository, *, quota: int = 2, gateway: FakeSheet | None = None
):
    gateway = gateway or FakeSheet()
    destination = ClientSheetDestinationStore(repo).register_google_sheet(
        client_id="client-a",
        destination_id="jobs",
        display_name="Client Jobs",
        spreadsheet="sheet123",
        tab_name="Jobs",
        column_mapping={
            "Job Title": "Role",
            "Company Name": "Company",
            "Job Link": "URL",
        },
        gateway=gateway,
    )
    store = ClientDeliveryProfileStore(repo)
    profile = store.upsert(
        client_id="client-a",
        destination_id="jobs",
        sourcing_plan_id="software-us-v1",
        daily_quota=quota,
        status="active",
        delivery_mode="review",
        timezone="Africa/Lagos",
    )
    return gateway, destination, store, profile


def prepare(repo: SQLiteRepository, destination, jobs: list[Job]):
    for posting in jobs:
        repo.upsert_job(posting)
        repo.save_match(
            JobMatch(
                job_id=posting.id,
                client_id="client-a",
                decision="strong_match",
                evaluated_at=datetime.now(UTC),
                matcher_version="test",
            )
        )
    ids = tuple(posting.id for posting in jobs)
    store = DailyBatchStore(repo)
    request = DailyBatchRequest(
        client_id="client-a",
        destination=destination.logical_uri,
        destination_id=destination.destination_id,
        destination_config_sha256=destination.config_sha256,
        idempotency_key="scope-1",
        requested_quota=len(jobs),
        max_jobs_per_employer_per_batch=1,
        max_posting_age_hours=24,
        unknown_posting_age_policy="reject",
        freshness_evaluated_at=datetime.now(UTC),
        evidence_scope_id="scope-1",
        evaluation_id="eval-1",
        candidate_job_ids=ids,
        evidence_sha256=store.evidence_digest("client-a", ids),
    )
    return prepare_daily_batch(repository=repo, request=request)


def test_release_guard_blocks_batch_after_quota_is_reduced(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    gateway, destination, store, profile = setup_profile(repo, quota=2)
    batch = prepare(
        repo,
        destination,
        [make_job("job-1", "Acme"), make_job("job-2", "Beta")],
    )
    assert batch.selected_count == 2

    profile = store.update_controls(profile, daily_quota=1)

    with pytest.raises(BatchConflict, match="remaining daily quota"):
        store.guard_batch_release(profile, batch.batch_id, gateway=gateway)


def test_release_guard_blocks_paused_profile(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    gateway, destination, store, profile = setup_profile(repo, quota=1)
    batch = prepare(repo, destination, [make_job("job-1", "Acme")])
    profile = store.update_controls(profile, status="paused")

    with pytest.raises(BatchConflict, match="paused"):
        store.guard_batch_release(profile, batch.batch_id, gateway=gateway)


def test_reconcile_before_release_blocks_manually_added_duplicate(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    gateway, destination, store, profile = setup_profile(repo, quota=2)
    posting = make_job("job-1", "Acme")
    batch = prepare(repo, destination, [posting])

    # The client adds the same job manually after the review batch was prepared.
    gateway.values.append([posting.title, posting.company, str(posting.canonical_url)])

    guarded, reconciliation, remaining = store.guard_batch_release(
        profile, batch.batch_id, gateway=gateway
    )
    assert guarded.batch_id == batch.batch_id
    assert reconciliation["observed_links"] == 1
    assert remaining == 1

    failed = finalize_daily_batch(
        repository=repo,
        batch_id=batch.batch_id,
        sheets_gateway=gateway,
    )
    assert failed.status == "failed"
    assert len(gateway.values) == 2

def test_release_guard_recovers_already_applied_uncertain_export_before_quota_check(tmp_path):
    class UncertainSheet(FakeSheet):
        def __init__(self):
            super().__init__()
            self.fail_once = True

        def append_table_rows(self, spreadsheet_id, tab, rows):
            super().append_table_rows(spreadsheet_id, tab, rows)
            if self.fail_once:
                self.fail_once = False
                raise OSError("simulated lost append response")

    repo = SQLiteRepository(tmp_path / "jobs.db")
    uncertain = UncertainSheet()
    gateway, destination, store, profile = setup_profile(
        repo, quota=1, gateway=uncertain
    )
    batch = prepare(repo, destination, [make_job("job-1", "Acme")])

    failed = finalize_daily_batch(
        repository=repo,
        batch_id=batch.batch_id,
        sheets_gateway=gateway,
    )
    assert failed.status == "failed"
    assert len(gateway.values) == 2

    guarded, reconciliation, remaining = store.guard_batch_release(
        profile, batch.batch_id, gateway=gateway
    )
    assert guarded.batch_id == batch.batch_id
    assert reconciliation["observed_links"] == 1
    assert remaining == 0

    recovered = finalize_daily_batch(
        repository=repo,
        batch_id=batch.batch_id,
        sheets_gateway=gateway,
    )
    assert recovered.status == "delivered"
    assert len(gateway.values) == 2
    assert store.delivered_today(profile) == 1

def test_generic_batch_release_rejects_profile_attributed_client_sheet_batch(
    tmp_path, monkeypatch, capsys
):
    database = tmp_path / "jobs.db"
    repo = SQLiteRepository(database)
    _gateway, destination, _store, _profile = setup_profile(repo, quota=1)
    batch = prepare(repo, destination, [make_job("job-1", "Acme")])

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "job-scout",
            "batch",
            "release",
            "--database",
            str(database),
            "--batch-id",
            batch.batch_id,
            "--confirm-batch-id",
            batch.batch_id,
        ],
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 2
    assert "delivery-profile release-batch" in capsys.readouterr().err
    assert DailyBatchStore(repo).get(batch.batch_id).status == "prepared"

def test_generic_batch_release_allows_registered_destination_without_profile(
    tmp_path, monkeypatch
):
    database = tmp_path / "jobs.db"
    repo = SQLiteRepository(database)
    gateway = FakeSheet()
    destination = ClientSheetDestinationStore(repo).register_google_sheet(
        client_id="client-a",
        destination_id="jobs",
        display_name="Client Jobs",
        spreadsheet="sheet123",
        tab_name="Jobs",
        column_mapping={
            "Job Title": "Role",
            "Company Name": "Company",
            "Job Link": "URL",
        },
        gateway=gateway,
    )
    batch = prepare(repo, destination, [make_job("job-1", "Acme")])
    called = []

    def fake_finalize(*, repository, batch_id, expected_generation_id):
        called.append((batch_id, expected_generation_id))
        current = DailyBatchStore(repository).get(batch_id)
        return current.model_copy(update={"status": "delivered"})

    monkeypatch.setattr("job_scout.cli.finalize_daily_batch", fake_finalize)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "job-scout",
            "batch",
            "release",
            "--database",
            str(database),
            "--batch-id",
            batch.batch_id,
            "--confirm-batch-id",
            batch.batch_id,
        ],
    )

    main()

    assert called == [(batch.batch_id, batch.generation_id)]

def test_profile_cli_release_binds_guarded_generation(tmp_path, monkeypatch):
    database = tmp_path / "jobs.db"
    repo = SQLiteRepository(database)
    gateway, destination, _store, _profile = setup_profile(repo, quota=1)
    batch = prepare(repo, destination, [make_job("job-1", "Acme")])
    called = []

    def fake_finalize(*, repository, batch_id, expected_generation_id):
        called.append((batch_id, expected_generation_id))
        current = DailyBatchStore(repository).get(batch_id)
        return current.model_copy(update={"status": "delivered"})

    monkeypatch.setattr("job_scout.cli.GoogleSheetsGateway", lambda: gateway)
    monkeypatch.setattr("job_scout.cli.finalize_daily_batch", fake_finalize)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "job-scout",
            "delivery-profile",
            "release-batch",
            "--database",
            str(database),
            "--profile-id",
            delivery_profile_control_id("client-a", "jobs"),
            "--batch-id",
            batch.batch_id,
            "--confirm-batch-id",
            batch.batch_id,
        ],
    )

    main()

    assert called == [(batch.batch_id, batch.generation_id)]

