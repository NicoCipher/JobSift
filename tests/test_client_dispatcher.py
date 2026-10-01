import sys
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from job_scout.client_dispatcher import (
    _release_quota_blocked,
    _remaining_quota,
    dispatch_profile,
    main,
    reconcile_sheet_history,
)
from job_scout.delivery_destinations import ClientSheetDestinationStore
from job_scout.delivery_profiles import ClientDeliveryProfile
from job_scout.storage.daily_batches import DailyBatchStore
from job_scout.storage.inventory_runs import InventoryRunStore
from job_scout.storage.sqlite import SQLiteRepository


class FakeSheet:
    def __init__(self):
        self.values = [
            ["JOB TITLE", "COMPANY NAME", "LINKS", "DESCRIPTION"],
            [
                "Backend Engineer",
                "Acme",
                "https://job-boards.greenhouse.io/acme/jobs/123",
                "Build services.",
            ],
            [
                "Frontend Engineer",
                "Beta",
                "https://jobs.ashbyhq.com/beta/123e4567-e89b-12d3-a456-426614174000/application",
                "Build interfaces.",
            ],
        ]
        self.title = "Sheet1"
        self.sheet_id = 0

    def sheet_metadata(self, spreadsheet_id):
        return [{"sheet_id": self.sheet_id, "title": self.title}]

    def read_rows(self, spreadsheet_id, tab):
        return [row.copy() for row in self.values]

    def read_table_rows(self, spreadsheet_id, tab):
        return [row.copy() for row in self.values]


def setup_destination(repo, gateway):
    return ClientSheetDestinationStore(repo).register_google_sheet(
        client_id="client-a",
        destination_id="primary",
        display_name="Primary Jobs",
        spreadsheet="sheet123",
        tab_name="Sheet1",
        column_mapping={
            "Job Title": "JOB TITLE",
            "Company Name": "COMPANY NAME",
            "Job Link": "LINKS",
            "Job Description": "DESCRIPTION",
        },
        gateway=gateway,
    )


def profile():
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    return ClientDeliveryProfile(
        client_id="client-a",
        profile_id="primary",
        destination_id="primary",
        brief_path="config/search_briefs/client-a.json",
        link_quota=100,
        quota_scope="sheet_total",
        delivery_mode="review",
        status="active",
        timezone="Africa/Lagos",
        created_at=now,
        updated_at=now,
    )


def test_sheet_reconciliation_suppresses_existing_links_and_counts_sheet_total(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    gateway = FakeSheet()
    destination = setup_destination(repo, gateway)
    value = profile()

    result = reconcile_sheet_history(
        repository=repo,
        profile=value,
        destination=destination,
        gateway=gateway,
    )

    assert result["sheet_links"] == 2
    with repo.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM historical_job_links WHERE client_id='client-a'"
            ).fetchone()[0]
            == 2
        )

    remaining, progress = _remaining_quota(
        repository=repo,
        profile=value,
        destination=destination,
        sheet_links=result["sheet_links"],
        now=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
    )
    assert progress == 2
    assert remaining == 98

    replay = reconcile_sheet_history(
        repository=repo,
        profile=value,
        destination=destination,
        gateway=gateway,
    )
    assert replay["history_inserted"] == 0
    assert replay["history_already_present"] == 2


def test_sheet_reconciliation_fails_closed_on_header_drift(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    gateway = FakeSheet()
    destination = setup_destination(repo, gateway)
    gateway.values[0][0] = "ROLE"

    with pytest.raises(ValueError, match="header differs"):
        reconcile_sheet_history(
            repository=repo,
            profile=profile(),
            destination=destination,
            gateway=gateway,
        )


def test_release_quota_recheck_blocks_new_append_but_allows_uncertain_recovery(
    tmp_path, monkeypatch
):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    gateway = FakeSheet()
    destination = setup_destination(repo, gateway)
    batch = SimpleNamespace(batch_id="batch-1", selected_count=3)

    monkeypatch.setattr(
        DailyBatchStore,
        "export_rows",
        lambda self, batch_id: [
            {"Job Link": "https://job-boards.greenhouse.io/gamma/jobs/789"}
        ],
    )
    assert _release_quota_blocked(
        repository=repo,
        batch=batch,
        destination=destination,
        gateway=gateway,
        remaining=1,
    )

    gateway.values.append(
        [
            "Platform Engineer",
            "Gamma",
            "https://job-boards.greenhouse.io/gamma/jobs/789",
            "Build platform systems.",
        ]
    )
    assert not _release_quota_blocked(
        repository=repo,
        batch=batch,
        destination=destination,
        gateway=gateway,
        remaining=0,
    )


def test_failed_inventory_run_is_reported_as_sourcing_failure(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    gateway = FakeSheet()
    destination = ClientSheetDestinationStore(repo).register_google_sheet(
        client_id="taiwo_operator_sourcing_v1",
        destination_id="primary",
        display_name="Primary Jobs",
        spreadsheet="sheet456",
        tab_name="Sheet1",
        column_mapping={
            "Job Title": "JOB TITLE",
            "Company Name": "COMPANY NAME",
            "Job Link": "LINKS",
            "Job Description": "DESCRIPTION",
        },
        gateway=gateway,
    )
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    value = ClientDeliveryProfile(
        client_id="taiwo_operator_sourcing_v1",
        profile_id="primary",
        destination_id=destination.destination_id,
        brief_path="config/search_briefs/taiwo_software_remote_us_v1.json",
        link_quota=100,
        quota_scope="sheet_total",
        delivery_mode="review",
        status="active",
        timezone="Africa/Lagos",
        created_at=now,
        updated_at=now,
    )
    inventory = InventoryRunStore(repo)
    inventory.create(run_id="failed-run", plan_id="rolling", started_at=now)
    inventory.finish(run_id="failed-run", status="failure", completed_at=now)

    result = dispatch_profile(
        repository=repo,
        profile=value,
        run_id="failed-run",
        gateway=gateway,
        now=now,
    )

    assert result["action"] == "sourcing_failed"
    assert result["inventory_status"] == "failure"


def test_pending_release_failure_exits_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "job_scout.client_dispatcher.GoogleSheetsGateway",
        lambda: FakeSheet(),
    )
    monkeypatch.setattr(
        "job_scout.client_dispatcher.manage_pending",
        lambda **kwargs: {
            "action": "release_failed",
            "batch_id": "batch-1",
            "error": "destination changed",
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "client-dispatcher",
            "--database",
            str(tmp_path / "jobs.db"),
            "pending",
            "--client",
            "client-a",
            "--action",
            "release",
        ],
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 1
    assert '"action": "release_failed"' in capsys.readouterr().out
