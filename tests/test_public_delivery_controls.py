from pathlib import Path
from types import SimpleNamespace

from job_scout.profile_delivery_runner import _public_result


def profile():
    return SimpleNamespace(
        client_id="secret-client",
        destination_id="private-sheet",
        daily_quota=100,
        delivery_mode="review",
    )


def test_public_profile_result_drops_identifying_and_job_detail_fields():
    result = {
        "action": "prepared",
        "client_id": "secret-client",
        "destination_id": "private-sheet",
        "destination": "client-sheet://private-sheet",
        "batch_id": "batch-123",
        "selected_count": 7,
        "shortfall": 93,
        "selected_jobs": [
            {
                "title": "Software Engineer",
                "company": "Example",
                "link": "https://example.com/job",
            }
        ],
        "delivery_profile": {
            "client_id": "secret-client",
            "destination_id": "private-sheet",
            "daily_quota": 100,
            "status": "active",
            "delivery_mode": "review",
            "timezone": "Africa/Lagos",
            "delivered_today": 14,
            "remaining_today": 86,
        },
        "sheet_reconciliation": {
            "observed_links": 14,
            "newly_recorded_links": 14,
        },
    }

    public = _public_result(profile(), result)
    serialized = str(public)

    assert public["batch_id"] == "batch-123"
    assert public["selected_count"] == 7
    assert public["delivery_profile"]["remaining_today"] == 86
    assert "secret-client" not in serialized
    assert "private-sheet" not in serialized
    assert "selected_jobs" not in public
    assert "example.com/job" not in serialized


def test_public_control_workflow_has_no_client_or_sheet_identifiers_as_inputs():
    workflow = Path(".github/workflows/client-delivery-control.yml").read_text(
        encoding="utf-8"
    )
    inputs = workflow.split("permissions:", 1)[0]

    assert "client_id" not in inputs
    assert "destination_id" not in inputs
    assert "spreadsheet" not in inputs
    assert "profile_id" in inputs
