from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

import pytest

from job_scout import cli
from job_scout.delivery_destinations import ClientSheetDestinationStore
from job_scout.delivery_profiles import (
    ClientDeliveryProfileStore,
    delivery_profile_control_id,
)
from job_scout.domain.daily_batch import BatchConflict
from job_scout.domain.models import Job
from job_scout.normalization.core import content_fingerprint
from job_scout.storage.daily_batches import DailyBatchStore
from job_scout.storage.sqlite import SQLiteRepository


class FakeSheet:
    def __init__(self):
        self.values = [["JOB TITLE", "COMPANY NAME", "LINKS", "DESCRIPTION"]]

    def sheet_metadata(self, spreadsheet_id):
        assert spreadsheet_id == "sheet123"
        return [{"sheet_id": 7, "title": "Sheet1"}]

    def read_rows(self, spreadsheet_id, tab):
        assert (spreadsheet_id, tab) == ("sheet123", "Sheet1")
        return [row.copy() for row in self.values]

    def read_table_rows(self, spreadsheet_id, tab):
        return self.read_rows(spreadsheet_id, tab)


def register(repo: SQLiteRepository, *, client: str = "client-a", destination: str = "jobs"):
    return ClientSheetDestinationStore(repo).register_google_sheet(
        client_id=client,
        destination_id=destination,
        display_name="Client Jobs",
        spreadsheet="sheet123",
        tab_name="Sheet1",
        column_mapping={
            "Job Title": "JOB TITLE",
            "Company Name": "COMPANY NAME",
            "Job Link": "LINKS",
            "Job Description": "DESCRIPTION",
        },
        gateway=FakeSheet(),
    )


def make_job(job_id: str = "job-1", *, apply_url: str | None = None) -> Job:
    now = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    description = "Build production software."
    return Job(
        id=job_id,
        source="greenhouse",
        source_job_id=job_id,
        source_board_id="acme",
        title="Software Engineer",
        company="Acme",
        employer_id="acme",
        description_text=description,
        job_url=f"https://example.com/{job_id}",
        apply_url=apply_url,
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


def test_profile_requires_registered_destination_and_persists_controls(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    store = ClientDeliveryProfileStore(repo)

    with pytest.raises(BatchConflict, match="destination"):
        store.upsert(
            client_id="client-a",
            destination_id="missing",
            sourcing_plan_id="remote-software-v1",
            daily_quota=100,
            status="active",
            delivery_mode="review",
            timezone="Africa/Lagos",
        )

    register(repo)
    profile = store.upsert(
        client_id="client-a",
        destination_id="jobs",
        sourcing_plan_id="remote-software-v1",
        daily_quota=100,
        status="active",
        delivery_mode="review",
        timezone="Africa/Lagos",
    )

    assert profile.daily_quota == 100
    assert profile.status == "active"
    assert profile.delivery_mode == "review"
    assert store.get("client-a", "jobs") == profile
    assert store.active() == (profile,)


def test_disabled_destination_cannot_be_activated(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    register(repo)
    ClientSheetDestinationStore(repo).disable("client-a", "jobs")

    with pytest.raises(BatchConflict, match="ready"):
        ClientDeliveryProfileStore(repo).upsert(
            client_id="client-a",
            destination_id="jobs",
            sourcing_plan_id="remote-software-v1",
            daily_quota=50,
            status="active",
            delivery_mode="auto",
            timezone="Africa/Lagos",
        )


def test_pause_preserves_other_profile_controls(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    register(repo)
    store = ClientDeliveryProfileStore(repo)
    profile = store.upsert(
        client_id="client-a",
        destination_id="jobs",
        sourcing_plan_id="remote-software-v1",
        daily_quota=150,
        status="active",
        delivery_mode="auto",
        timezone="Africa/Lagos",
    )

    paused = store.set_status("client-a", "jobs", "paused")

    assert paused.status == "paused"
    assert paused.daily_quota == profile.daily_quota
    assert paused.delivery_mode == profile.delivery_mode
    assert paused.sourcing_plan_id == profile.sourcing_plan_id
    assert store.active() == ()


def test_delivered_today_is_scoped_to_destination_and_profile_timezone(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    destination = register(repo)
    store = ClientDeliveryProfileStore(repo)
    profile = store.upsert(
        client_id="client-a",
        destination_id="jobs",
        sourcing_plan_id="remote-software-v1",
        daily_quota=3,
        status="active",
        delivery_mode="review",
        timezone="Africa/Lagos",
    )

    posting = make_job()
    repo.upsert_job(posting)

    # 23:30 UTC on Sep 30 is 00:30 Oct 1 in Lagos and must count for Oct 1.
    # Quota is defined by the destination link that was actually observed.
    with repo.connect() as connection:
        connection.execute(
            "INSERT INTO destination_observed_links "
            "(client_id,destination,normalized_url,source,source_board_id,"
            "source_job_id,first_observed_at) VALUES (?,?,?,?,?,?,?)",
            (
                "client-a",
                destination.logical_uri,
                str(posting.canonical_url),
                posting.source,
                posting.source_board_id,
                posting.source_job_id,
                "2026-09-30T23:30:00+00:00",
            ),
        )

    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    assert store.delivered_today(profile, now=now) == 1
    assert store.remaining_today(profile, now=now) == 2


def test_reconciliation_makes_existing_sheet_link_prior_surfacing(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    gateway = FakeSheet()
    existing = make_job()
    gateway.values.append(
        [
            existing.title,
            existing.company,
            str(existing.canonical_url),
            "Already present in the client Sheet",
        ]
    )
    register(repo)
    store = ClientDeliveryProfileStore(repo)
    profile = store.upsert(
        client_id="client-a",
        destination_id="jobs",
        sourcing_plan_id="remote-software-v1",
        daily_quota=100,
        status="active",
        delivery_mode="review",
        timezone="Africa/Lagos",
    )

    first = store.reconcile_destination_sheet(profile, gateway=gateway)
    second = store.reconcile_destination_sheet(profile, gateway=gateway)

    assert first == {"observed_links": 1, "newly_recorded_links": 1}
    assert second == {"observed_links": 1, "newly_recorded_links": 0}
    destination = ClientSheetDestinationStore(repo).get("client-a", "jobs")
    assert repo.is_historically_surfaced(
        existing, "client-a", destination.logical_uri
    )
    assert store.delivered_today(profile) == 1

    # If the same link is also journaled as an automated delivery, quota
    # accounting still counts the URL once.
    repo.upsert_job(existing)
    group_id = repo.delivery_group_id(existing.id)
    with repo.connect() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO group_deliveries VALUES (?,?,?,?,?)",
            (
                group_id,
                "client-a",
                destination.logical_uri,
                existing.id,
                datetime.now(UTC).isoformat(),
            ),
        )
    assert store.delivered_today(profile) == 1


def test_quota_counts_exported_apply_url_once_after_sheet_reconciliation(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    destination = register(repo)
    profile_store = ClientDeliveryProfileStore(repo)
    profile = profile_store.upsert(
        client_id="client-a",
        destination_id="jobs",
        sourcing_plan_id="remote-software-v1",
        daily_quota=10,
        status="active",
        delivery_mode="review",
        timezone="Africa/Lagos",
    )

    posting = make_job(
        "job-apply",
        apply_url="https://apply.example.com/jobs/job-apply",
    )
    repo.upsert_job(posting)
    group_id = repo.delivery_group_id(posting.id)
    DailyBatchStore(repo)
    now = datetime.now(UTC).isoformat()

    repo.observe_destination_links(
        client_id="client-a",
        destination=destination.logical_uri,
        links=[str(posting.apply_url)],
    )
    with repo.connect() as connection:
        connection.execute(
            "INSERT INTO group_deliveries VALUES (?,?,?,?,?)",
            (
                group_id,
                "client-a",
                destination.logical_uri,
                posting.id,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO daily_batches "
            "(batch_id,client_id,destination,idempotency_key,requested_quota,"
            "selected_count,shortfall,status,assembled_at,delivered_at,request_json,"
            "counts_json,dedupe_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "batch-apply",
                "client-a",
                destination.logical_uri,
                "scope-apply",
                1,
                1,
                0,
                "delivered",
                now,
                now,
                "{}",
                "{}",
                "test",
            ),
        )
        connection.execute(
            "INSERT INTO daily_batch_items VALUES (?,?,?,?,?,?,?)",
            (
                "batch-apply",
                1,
                group_id,
                posting.id,
                "a" * 64,
                "test",
                json.dumps({"Job Link": str(posting.apply_url)}),
            ),
        )

    # Reconciliation observes the exact apply URL written to the Sheet, while
    # the delivery journal points at the same posting whose canonical URL is
    # different. The quota must still count one delivered link, not two.
    assert profile_store.delivered_today(profile) == 1


def test_quota_uses_current_group_after_frozen_group_merge(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    destination = register(repo)
    store = ClientDeliveryProfileStore(repo)
    profile = store.upsert(
        client_id="client-a",
        destination_id="jobs",
        sourcing_plan_id="remote-software-v1",
        daily_quota=10,
        status="active",
        delivery_mode="review",
        timezone="Africa/Lagos",
    )

    posting = make_job("merged-group-job")
    repo.upsert_job(posting)
    current_group = repo.delivery_group_id(posting.id)
    DailyBatchStore(repo)
    now = datetime.now(UTC).isoformat()

    repo.observe_destination_links(
        client_id="client-a",
        destination=destination.logical_uri,
        links=[str(posting.canonical_url)],
    )
    with repo.connect() as connection:
        connection.execute(
            "INSERT INTO group_deliveries VALUES (?,?,?,?,?)",
            (
                current_group,
                "client-a",
                destination.logical_uri,
                posting.id,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO daily_batches "
            "(batch_id,client_id,destination,idempotency_key,requested_quota,"
            "selected_count,shortfall,status,assembled_at,delivered_at,request_json,"
            "counts_json,dedupe_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "batch-merged-group",
                "client-a",
                destination.logical_uri,
                "scope-merged-group",
                1,
                1,
                0,
                "delivered",
                now,
                now,
                "{}",
                "{}",
                "test",
            ),
        )
        # Simulate a batch frozen before its original delivery group was merged
        # into the posting's current group.
        connection.execute(
            "INSERT INTO daily_batch_items VALUES (?,?,?,?,?,?,?)",
            (
                "batch-merged-group",
                1,
                "obsolete-frozen-group",
                posting.id,
                "a" * 64,
                "test",
                json.dumps({"Job Link": str(posting.canonical_url)}),
            ),
        )

    assert store.delivered_today(profile) == 1


def test_legacy_delivery_quota_survives_later_apply_url_change(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    destination = register(repo)
    store = ClientDeliveryProfileStore(repo)
    profile = store.upsert(
        client_id="client-a",
        destination_id="jobs",
        sourcing_plan_id="remote-software-v1",
        daily_quota=10,
        status="active",
        delivery_mode="review",
        timezone="Africa/Lagos",
    )

    original = make_job("legacy-job")
    repo.upsert_job(original)
    group_id = repo.delivery_group_id(original.id)
    repo.observe_destination_links(
        client_id="client-a",
        destination=destination.logical_uri,
        links=[str(original.canonical_url)],
    )
    with repo.connect() as connection:
        connection.execute(
            "INSERT INTO group_deliveries VALUES (?,?,?,?,?)",
            (
                group_id,
                "client-a",
                destination.logical_uri,
                original.id,
                datetime.now(UTC).isoformat(),
            ),
        )

    # A later collection learns a new apply URL. The historical Sheet row still
    # contains the canonical URL that was actually exported.
    refreshed = make_job(
        "legacy-job",
        apply_url="https://apply.example.com/jobs/legacy-job",
    )
    repo.upsert_job(refreshed)

    assert store.delivered_today(profile) == 1


def test_legacy_exported_apply_url_remains_one_quota_link_after_url_changes(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    destination = register(repo)
    store = ClientDeliveryProfileStore(repo)
    profile = store.upsert(
        client_id="client-a",
        destination_id="jobs",
        sourcing_plan_id="remote-software-v1",
        daily_quota=10,
        status="active",
        delivery_mode="review",
        timezone="Africa/Lagos",
    )

    original = make_job(
        "legacy-apply-job",
        apply_url="https://external.example/apply/old-token",
    )
    repo.upsert_job(original)
    group_id = repo.delivery_group_id(original.id)

    # This is the URL that actually exists on the client Sheet.
    repo.observe_destination_links(
        client_id="client-a",
        destination=destination.logical_uri,
        links=[str(original.apply_url)],
    )
    with repo.connect() as connection:
        connection.execute(
            "INSERT INTO group_deliveries VALUES (?,?,?,?,?)",
            (
                group_id,
                "client-a",
                destination.logical_uri,
                original.id,
                datetime.now(UTC).isoformat(),
            ),
        )

    # A later provider refresh changes the apply URL. Quota accounting must not
    # invent the new URL or count the one Sheet row twice.
    refreshed = make_job(
        "legacy-apply-job",
        apply_url="https://external.example/apply/new-token",
    )
    repo.upsert_job(refreshed)

    assert store.delivered_today(profile) == 1


def test_control_id_is_stable_opaque_and_resolves_profile(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    register(
        repo,
        client="sensitive-client-name",
        destination="private-sheet-destination",
    )
    store = ClientDeliveryProfileStore(repo)
    profile = store.upsert(
        client_id="sensitive-client-name",
        destination_id="private-sheet-destination",
        sourcing_plan_id="remote-software-v1",
        daily_quota=100,
        status="active",
        delivery_mode="review",
        timezone="Africa/Lagos",
    )

    control_id = delivery_profile_control_id(
        profile.client_id, profile.destination_id
    )

    assert len(control_id) == 16
    assert "sensitive" not in control_id
    assert "private" not in control_id
    assert store.get_by_control_id(control_id) == profile
    assert store.public_status(profile)["profile_id"] == control_id
    assert "client_id" not in store.public_status(profile)
    assert "destination_id" not in store.public_status(profile)


def test_cli_sheet_controls_use_opaque_profile_id_and_fail_safe(tmp_path, monkeypatch, capsys):
    database = tmp_path / "jobs.db"
    repo = SQLiteRepository(database)
    register(repo)
    store = ClientDeliveryProfileStore(repo)
    profile = store.upsert(
        client_id="client-a",
        destination_id="jobs",
        sourcing_plan_id="remote-software-v1",
        daily_quota=100,
        status="active",
        delivery_mode="review",
        timezone="Africa/Lagos",
    )
    control_id = delivery_profile_control_id(
        profile.client_id, profile.destination_id
    )

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "job-scout",
            "delivery-profile",
            "sheet-disable",
            "--database",
            str(database),
            "--profile-id",
            control_id,
        ],
    )
    cli.main()
    disabled_payload = json.loads(capsys.readouterr().out)
    assert disabled_payload == {
        "profile_id": control_id,
        "profile_status": "paused",
        "sheet_status": "disabled",
    }
    serialized = json.dumps(disabled_payload)
    assert "client-a" not in serialized
    assert "jobs" not in serialized

    monkeypatch.setattr(cli, "GoogleSheetsGateway", FakeSheet)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "job-scout",
            "delivery-profile",
            "sheet-check",
            "--database",
            str(database),
            "--profile-id",
            control_id,
        ],
    )
    cli.main()
    disabled_check = json.loads(capsys.readouterr().out)
    assert disabled_check == {
        "profile_id": control_id,
        "profile_status": "paused",
        "sheet_status": "disabled",
        "observed_links": 0,
        "newly_recorded_links": 0,
    }

    # Model a legacy/inconsistent state where a disabled Sheet still has an
    # active profile. Re-enable must fail safe by pausing it before verification.
    with repo.connect() as connection:
        connection.execute(
            "UPDATE client_delivery_profiles SET status='active' "
            "WHERE client_id=? AND destination_id=?",
            ("client-a", "jobs"),
        )

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "job-scout",
            "delivery-profile",
            "sheet-enable",
            "--database",
            str(database),
            "--profile-id",
            control_id,
        ],
    )
    cli.main()
    enabled_payload = json.loads(capsys.readouterr().out)
    assert enabled_payload == {
        "profile_id": control_id,
        "profile_status": "paused",
        "sheet_status": "ready",
    }

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "job-scout",
            "delivery-profile",
            "sheet-check",
            "--database",
            str(database),
            "--profile-id",
            control_id,
        ],
    )
    cli.main()
    checked_payload = json.loads(capsys.readouterr().out)
    assert checked_payload["profile_id"] == control_id
    assert checked_payload["profile_status"] == "paused"
    assert checked_payload["sheet_status"] == "ready"
    assert checked_payload["observed_links"] == 0
    assert checked_payload["newly_recorded_links"] == 0


def test_observed_sheet_links_are_scoped_to_one_destination(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    posting = make_job()
    repo.upsert_job(posting)

    repo.observe_destination_links(
        client_id="client-a",
        destination="client-sheet://sheet-a",
        links=[str(posting.canonical_url)],
    )

    assert repo.is_historically_surfaced(
        posting, "client-a", "client-sheet://sheet-a"
    )
    assert not repo.is_historically_surfaced(
        posting, "client-a", "client-sheet://sheet-b"
    )
