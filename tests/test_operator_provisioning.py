from __future__ import annotations

from contextlib import contextmanager
from uuid import uuid4

import pytest

from job_scout.delivery_destinations import ClientSheetDestinationStore
from job_scout.delivery_profiles import ClientDeliveryProfileStore
from job_scout.operator_clients import OperatorClientStore, OperatorProvisioningStore
from job_scout.operator_provisioning import inspect_sheet, process_request, propose_mapping
from job_scout.storage.sqlite import SQLiteRepository


class FakeSheet:
    def sheet_metadata(self, spreadsheet_id):
        assert spreadsheet_id == "sheet123456"
        return [
            {"sheet_id": 17, "title": "Jobs"},
            {"sheet_id": 18, "title": "Archive"},
        ]

    def read_rows(self, spreadsheet_id, tab):
        assert (spreadsheet_id, tab) == ("sheet123456", "Jobs")
        return [["Role", "Company", "URL", "Source", "Notes"]]


def create_payload():
    return {
        "client_name": "Acme Software",
        "criteria": {
            "role_titles": ["Software Engineer", "Backend Developer"],
            "country": "US",
            "work_modes": ["remote"],
            "exclusions": ["Director"],
            "preferred_terms": ["Python"],
            "freshness_hours": 24,
        },
        "daily_limit": 250,
        "delivery_mode": "review",
        "sheet": {
            "url": "https://docs.google.com/spreadsheets/d/sheet123456/edit",
            "tab": "Jobs",
            "column_mapping": {
                "Job Title": "Role",
                "Company Name": "Company",
                "Job Link": "URL",
                "Job Platform": "Source",
            },
        },
    }


def test_sheet_inspection_proposes_required_mapping():
    inspection = inspect_sheet(
        sheet_url="https://docs.google.com/spreadsheets/d/sheet123456/edit",
        tab="Jobs",
        gateway=FakeSheet(),
    )

    assert inspection["tabs"] == ["Jobs", "Archive"]
    assert inspection["headers"] == ["Role", "Company", "URL", "Source", "Notes"]
    assert inspection["proposed_mapping"] == {
        "Job Title": "Role",
        "Company Name": "Company",
        "Job Link": "URL",
        "Job Platform": "Source",
    }
    assert inspection["missing_required_fields"] == []
    assert propose_mapping(["Position", "Employer", "Apply Link"]) == {
        "Job Title": "Position",
        "Company Name": "Employer",
        "Job Link": "Apply Link",
    }


def test_create_client_persists_sheet_brief_and_active_profile(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    request_id = str(uuid4())

    result = process_request(
        repo,
        request_id=request_id,
        operation="create_client",
        payload=create_payload(),
        gateway=FakeSheet(),
    )

    request = OperatorProvisioningStore(repo).get(request_id)
    assert request is not None
    assert request.state == "completed"
    assert result["client_name"] == "Acme Software"
    assert result["sheet_status"] == "ready"
    assert result["delivery_mode"] == "review"
    assert result["daily_limit"] == 250

    clients = OperatorClientStore(repo).list()
    assert len(clients) == 1
    client = clients[0]
    assert client.display_name == "Acme Software"
    assert client.brief.target_roles == ["Software Engineer", "Backend Developer"]
    assert client.brief.target_market.countries == {"US"}
    assert client.brief.target_market.unknown_policy == "reject"
    assert client.brief.work_mode.modes == {"remote"}
    assert client.brief.posting_freshness.max_age_hours == 24
    assert client.brief.posting_freshness.unknown_policy == "reject"
    assert client.brief.avoid_terms == ["Director"]
    assert client.brief.preferred_terms == ["Python"]

    destination = ClientSheetDestinationStore(repo).get(
        client.client_id,
        client.destination_id,
    )
    assert destination.status == "ready"
    assert destination.sheet_id == 17
    assert destination.column_mapping["Job Link"] == "URL"

    profile = ClientDeliveryProfileStore(repo).get(
        client.client_id,
        client.destination_id,
    )
    assert profile.status == "active"
    assert profile.delivery_mode == "review"
    assert profile.daily_quota == 250
    assert profile.sourcing_plan_id == client.sourcing_plan_id


def test_create_client_retry_returns_same_completed_result(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    request_id = str(uuid4())
    payload = create_payload()

    first = process_request(
        repo,
        request_id=request_id,
        operation="create_client",
        payload=payload,
        gateway=FakeSheet(),
    )
    second = process_request(
        repo,
        request_id=request_id,
        operation="create_client",
        payload=payload,
        gateway=FakeSheet(),
    )

    assert second == first
    assert len(OperatorClientStore(repo).list()) == 1


def test_create_client_rolls_back_activation_if_request_completion_loses_race(
    tmp_path,
    monkeypatch,
):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    request_id = str(uuid4())

    original_connect = repo.connect

    class WrappedConnection:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def __enter__(self):
            self.inner.__enter__()
            return self

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

        def execute(self, sql, params=()):
            if (
                "UPDATE operator_provisioning_requests" in sql
                and "state='completed'" in sql
            ):
                result = self.inner.execute(sql, params)

                class LostRace:
                    rowcount = 0

                return LostRace()
            return self.inner.execute(sql, params)

    @contextmanager
    def connect():
        with original_connect() as connection:
            yield WrappedConnection(connection)

    monkeypatch.setattr(repo, "connect", connect)

    with pytest.raises(ValueError, match="state changed before activation"):
        process_request(
            repo,
            request_id=request_id,
            operation="create_client",
            payload=create_payload(),
            gateway=FakeSheet(),
        )

    with original_connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM operator_clients"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM client_delivery_profiles"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM client_sheet_destinations"
            ).fetchone()[0]
            == 1
        )
