from __future__ import annotations

from uuid import uuid4

from job_scout.domain.models import SearchBrief
from job_scout.operator_clients import (
    OperatorClientStore,
    OperatorProvisioningStore,
    brief_sha256,
)
from job_scout.storage.sqlite import SQLiteRepository


def brief(client_id: str, role: str = "Software Engineer") -> SearchBrief:
    return SearchBrief.model_validate(
        {
            "client_id": client_id,
            "target_roles": [role],
            "target_market": {
                "countries": ["US"],
                "intent": "must",
                "unknown_policy": "reject",
            },
            "work_mode": {
                "modes": ["remote"],
                "intent": "must",
                "unknown_policy": "reject",
            },
            "posting_freshness": {
                "max_age_hours": 24,
                "unknown_policy": "reject",
            },
        }
    )


def test_operator_client_store_persists_human_config_and_revisions(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    store = OperatorClientStore(repo)
    first_brief = brief("client-a")

    first = store.upsert(
        client_id="client-a",
        display_name="Acme Search",
        destination_id="jobs",
        destination_name="Acme Jobs",
        sourcing_plan_id="operator-client-a",
        brief=first_brief,
    )

    assert first.display_name == "Acme Search"
    assert first.destination_name == "Acme Jobs"
    assert first.brief_revision == 1
    assert first.brief_sha256 == brief_sha256(first_brief)
    assert store.get_for_profile("client-a", "jobs") == first

    second_brief = brief("client-a", "Backend Engineer")
    second = store.upsert(
        client_id="client-a",
        display_name="Acme Search",
        destination_id="jobs",
        destination_name="Acme Jobs",
        sourcing_plan_id="operator-client-a",
        brief=second_brief,
    )

    assert second.brief_revision == 2
    assert second.brief.target_roles == ["Backend Engineer"]
    assert second.brief_sha256 != first.brief_sha256
    assert [item.client_id for item in store.list()] == ["client-a"]


def test_provisioning_request_is_idempotent_after_completion(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    store = OperatorProvisioningStore(repo)
    request_id = str(uuid4())
    payload = {
        "client_name": "Acme",
        "sheet_url": "https://docs.google.com/spreadsheets/d/example123456789/edit",
        "tab": "Jobs",
    }

    created = store.create(
        request_id=request_id,
        operation="inspect_sheet",
        payload=payload,
    )
    assert created.state == "queued"
    assert created.payload == payload

    claimed = store.claim(request_id)
    assert claimed.state == "running"

    completed = store.complete(
        request_id,
        {"tabs": ["Jobs"], "headers": ["Role", "Company", "URL"]},
    )
    assert completed.state == "completed"
    assert store.claim(request_id) == completed


def test_failed_provisioning_request_can_be_retried(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    store = OperatorProvisioningStore(repo)
    request_id = str(uuid4())
    store.create(
        request_id=request_id,
        operation="create_client",
        payload={"client_name": "Acme"},
    )
    store.claim(request_id)
    failed = store.fail(
        request_id,
        code="SHEET_NOT_READY",
        message="Jobs tab is missing.",
    )
    assert failed.state == "failed"
    assert failed.error_code == "SHEET_NOT_READY"

    retried = store.claim(request_id)
    assert retried.state == "running"
    assert retried.error_code is None
