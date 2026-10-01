import pytest

from job_scout.delivery_destinations import ClientSheetDestinationStore
from job_scout.delivery_profiles import ClientDeliveryProfileStore
from job_scout.storage.sqlite import SQLiteRepository


class FakeSheet:
    def __init__(self):
        self.values = [["JOB TITLE", "COMPANY NAME", "LINKS", "DESCRIPTION"]]

    def sheet_metadata(self, spreadsheet_id):
        return [{"sheet_id": 0, "title": "Sheet1"}]

    def read_rows(self, spreadsheet_id, tab):
        return [row.copy() for row in self.values]


def destination(repo):
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
            "Description": "DESCRIPTION",
        },
        gateway=FakeSheet(),
    )


def test_profile_controls_destination_quota_mode_and_pause(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    destination(repo)
    store = ClientDeliveryProfileStore(repo)

    profile = store.configure(
        client_id="client-a",
        profile_id="primary",
        destination_id="primary",
        brief_path="config/search_briefs/client-a.json",
        link_quota=100,
        quota_scope="sheet_total",
        delivery_mode="review",
        status="active",
        timezone="Africa/Lagos",
    )

    assert profile.link_quota == 100
    assert profile.quota_scope == "sheet_total"
    assert profile.delivery_mode == "review"
    assert [value.profile_id for value in store.active()] == ["primary"]

    paused = store.set_status("client-a", "primary", "paused")
    assert paused.status == "paused"
    assert store.active() == ()


def test_profile_rejects_unregistered_destination_and_unsafe_brief(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    store = ClientDeliveryProfileStore(repo)

    with pytest.raises(ValueError):
        store.configure(
            client_id="client-a",
            profile_id="primary",
            destination_id="missing",
            brief_path="config/search_briefs/client-a.json",
            link_quota=25,
            quota_scope="daily",
            delivery_mode="auto",
            status="active",
            timezone="Africa/Lagos",
        )

    destination(repo)
    with pytest.raises(ValueError, match="config/search_briefs"):
        store.configure(
            client_id="client-a",
            profile_id="primary",
            destination_id="primary",
            brief_path="../secret.json",
            link_quota=25,
            quota_scope="daily",
            delivery_mode="auto",
            status="active",
            timezone="Africa/Lagos",
        )
