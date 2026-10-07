from __future__ import annotations

from datetime import UTC, datetime

from job_scout.delivery_destinations import ClientSheetDestinationStore
from job_scout.delivery_profiles import ClientDeliveryProfileStore
from job_scout.domain.daily_batch import DailyBatchRequest
from job_scout.domain.models import Job, JobMatch
from job_scout.normalization.core import content_fingerprint
from job_scout.operator_snapshot import snapshot
from job_scout.orchestration.daily_batch import prepare_daily_batch
from job_scout.storage.daily_batches import DailyBatchStore
from job_scout.storage.sqlite import SQLiteRepository


class FakeSheet:
    def sheet_metadata(self, spreadsheet_id):
        return [{"sheet_id": 7, "title": "Sheet1"}]

    def read_rows(self, spreadsheet_id, tab):
        return [["JOB TITLE", "COMPANY NAME", "LINKS", "DESCRIPTION"]]


def test_snapshot_keeps_prepared_batch_when_profile_is_paused(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JOBSIFT_CONTROL_REQUEST_ID",
        "01234567-89ab-4cde-8fab-0123456789ab",
    )
    repo = SQLiteRepository(tmp_path / "jobs.db")
    destination = ClientSheetDestinationStore(repo).register_google_sheet(
        client_id="client-a",
        destination_id="jobs",
        display_name="Acme Jobs",
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
    profiles = ClientDeliveryProfileStore(repo)
    profiles.upsert(
        client_id="client-a",
        destination_id="jobs",
        sourcing_plan_id="remote-software-v1",
        daily_quota=100,
        status="active",
        delivery_mode="review",
        timezone="Africa/Lagos",
    )

    now = datetime(2026, 10, 6, 18, 0, tzinfo=UTC)
    job = Job(
        id="job-1",
        source="greenhouse",
        source_job_id="job-1",
        source_board_id="acme",
        title="Software Engineer",
        company="Acme",
        employer_id="acme",
        description_text="Build software.",
        job_url="https://example.com/job-1",
        apply_url="https://example.com/job-1/apply",
        canonical_url="https://example.com/job-1",
        posted_at=now,
        discovered_at=now,
        last_seen_at=now,
        content_fingerprint=content_fingerprint(
            title="Software Engineer",
            description="Build software.",
            location=None,
            employment_type=None,
        ),
    )
    repo.upsert_job(job)
    repo.save_match(
        JobMatch(
            job_id=job.id,
            client_id="client-a",
            decision="strong_match",
            evaluated_at=now,
            matcher_version="test",
        )
    )
    batches = DailyBatchStore(repo)
    prepared = prepare_daily_batch(
        repository=repo,
        request=DailyBatchRequest(
            client_id="client-a",
            destination=destination.logical_uri,
            destination_id=destination.destination_id,
            destination_config_sha256=destination.config_sha256,
            idempotency_key="operator-snapshot",
            requested_quota=1,
            evidence_scope_id="operator-snapshot",
            evaluation_id="operator-snapshot",
            candidate_job_ids=(job.id,),
            evidence_sha256=batches.evidence_digest("client-a", (job.id,)),
        ),
    )
    profiles.set_status("client-a", "jobs", "paused")

    state = snapshot(repo)
    assert state["schema_version"] == "operator-state-v1"
    assert state["control_request_id"] == "01234567-89ab-4cde-8fab-0123456789ab"
    assert state["truncated"] is False
    row = state["profiles"][0]
    assert row["profile_status"] == "paused"
    assert row["pending_batch"]["batch_id"] == prepared.batch_id
    assert row["pending_batch"]["selected_count"] == 1
    assert row["pending_batch"]["counts"]["selected_groups"] == 1
    assert row["pending_batch"]["preview"] == [
        {
            "ordinal": 1,
            "title": "Software Engineer",
            "company": "Acme",
            "link": "https://example.com/job-1/apply",
            "platform": "Greenhouse",
        }
    ]
    assert row["pending_batch"]["preview_truncated"] is False
    assert row["pending_batch"]["recovery_required"] is False

    with repo.connect() as connection:
        connection.execute(
            "UPDATE daily_batches SET status='failed',export_before_sha256=?,"
            "export_after_sha256=?,error=? WHERE batch_id=?",
            ("a" * 64, "b" * 64, "Google Sheets append outcome uncertain", prepared.batch_id),
        )

    recovery = snapshot(repo)["profiles"][0]["pending_batch"]
    assert recovery["batch_id"] == prepared.batch_id
    assert recovery["status"] == "failed"
    assert recovery["recovery_required"] is True
