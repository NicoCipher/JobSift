import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from job_scout.collectors.greenhouse import GreenhouseCollector
from job_scout.domain.models import CandidateProfile, Job, SourceTarget
from job_scout.export.csv_exporter import CSV_COLUMNS
from job_scout.normalization.core import content_fingerprint
from job_scout.orchestration.pipeline import run_pipeline
from job_scout.storage.inventory_runs import InventoryRunStore
from job_scout.storage.sqlite import SQLiteRepository

FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_jobs.json"


def test_fixture_to_csv_is_idempotent(tmp_path) -> None:
    payload = json.loads(FIXTURE.read_text())
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload, request=request)
    )
    collector = GreenhouseCollector(httpx.Client(transport=transport))
    profile = CandidateProfile.model_validate_json(Path("config/clients/example.json").read_text())
    profile.countries = set()
    profile.employment_types = set()
    repo = SQLiteRepository(tmp_path / "jobs.db")
    csv_path = tmp_path / "jobs.csv"
    first = run_pipeline(
        collector=collector,
        target=SourceTarget(board_id="acme", company="Acme"),
        profile=profile,
        repository=repo,
        csv_path=csv_path,
    )
    assert first.received == 2 and first.exported == 1 and first.rejected == 1
    with csv_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == CSV_COLUMNS
        assert len(list(reader)) == 1
    second = run_pipeline(
        collector=collector,
        target=SourceTarget(board_id="acme", company="Acme"),
        profile=profile,
        repository=repo,
        csv_path=csv_path,
    )
    assert second.exported == 0 and second.unchanged == 2
    with csv_path.open(newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 1


def test_failure_does_not_change_job_lifecycle(tmp_path) -> None:
    repo = SQLiteRepository(tmp_path / "jobs.db")
    transport = httpx.MockTransport(lambda request: httpx.Response(503, request=request))
    result = run_pipeline(
        collector=GreenhouseCollector(httpx.Client(transport=transport)),
        target=SourceTarget(board_id="acme", company="Acme"),
        profile=CandidateProfile.model_validate_json(
            Path("config/clients/example.json").read_text()
        ),
        repository=repo,
        csv_path=tmp_path / "jobs.csv",
    )
    assert result.status == "provider_error"
    with repo.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0

def _stored_job(identifier: str, *, posted_at: datetime) -> Job:
    return Job(
        id=identifier,
        source="greenhouse",
        source_job_id=identifier,
        source_board_id="acme",
        title="Backend Engineer",
        company="Acme",
        employer_id="acme",
        description_text="A large description that should not survive stale compaction.",
        description_html="<p>A large description that should not survive stale compaction.</p>",
        job_url=f"https://example.com/jobs/{identifier}",
        canonical_url=f"https://example.com/jobs/{identifier}",
        posted_at=posted_at,
        content_fingerprint=content_fingerprint(
            title="Backend Engineer",
            description="A large description that should not survive stale compaction.",
            location=None,
            employment_type=None,
        ),
        raw_metadata={"large": "payload"},
    )


def test_stale_inventory_pruning_cascades_shared_run_membership(tmp_path) -> None:
    repo = SQLiteRepository(tmp_path / "jobs.db")
    now = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
    posting = _stored_job("old-run-job", posted_at=now - timedelta(hours=73))
    repo.upsert_job(posting)

    inventory = InventoryRunStore(repo)
    inventory.create(run_id="run-1", plan_id="shared", started_at=now - timedelta(hours=1))
    inventory.add_jobs(
        run_id="run-1",
        target_identity="greenhouse:acme",
        jobs=[posting],
    )
    inventory.finish(run_id="run-1", status="success", completed_at=now)

    result = repo.prune_stale_inventory(retention_hours=72, now=now)

    assert result["deleted_jobs"] == 1
    with repo.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM inventory_run_jobs WHERE run_id='run-1'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT status FROM inventory_runs WHERE run_id='run-1'"
        ).fetchone()[0] == "success"


def test_stale_inventory_deletes_undelivered_and_compacts_delivered_jobs(tmp_path) -> None:
    repo = SQLiteRepository(tmp_path / "jobs.db")
    now = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
    old = now - timedelta(hours=73)
    undelivered = _stored_job("old-undelivered", posted_at=old)
    delivered = _stored_job("old-delivered", posted_at=old)
    repo.upsert_job(undelivered)
    repo.upsert_job(delivered)
    repo.mark_exported(delivered.id, "client", "gsheet://sheet/Sheet1")

    result = repo.prune_stale_inventory(retention_hours=72, now=now)

    assert result["deleted_jobs"] == 1
    assert result["compacted_jobs"] == 1
    with repo.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM jobs WHERE id=?", (undelivered.id,)
        ).fetchone() is None
        delivered_row = connection.execute(
            "SELECT payload_json,lifecycle FROM jobs WHERE id=?", (delivered.id,)
        ).fetchone()
        compact = Job.model_validate_json(delivered_row["payload_json"])
        assert compact.description_text is None
        assert compact.description_html is None
        assert compact.raw_metadata == {}
        assert delivered_row["lifecycle"] == "closed"
        ledger = connection.execute(
            "SELECT was_delivered FROM job_identity_ledger ORDER BY source_job_id"
        ).fetchall()
        assert [row[0] for row in ledger] == [1, 0]

def test_prune_skips_already_compacted_closed_payloads(tmp_path) -> None:
    repo = SQLiteRepository(tmp_path / "jobs.db")
    now = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
    delivered = _stored_job(
        "closed-payload", posted_at=now - timedelta(hours=80)
    )
    repo.upsert_job(delivered)
    repo.mark_exported(delivered.id, "client", "gsheet://sheet/Sheet1")

    first = repo.prune_stale_inventory(retention_hours=72, now=now)
    assert first["compacted_jobs"] == 1

    # Closed rows must not be reparsed on every maintenance pass.
    with repo.connect() as connection:
        connection.execute(
            "UPDATE jobs SET payload_json=? WHERE id=?",
            ("not-json", delivered.id),
        )

    second = repo.prune_stale_inventory(retention_hours=72, now=now)

    assert second["deleted_jobs"] == 0
    assert second["compacted_jobs"] == 0
    with repo.connect() as connection:
        assert connection.execute(
            "SELECT lifecycle FROM jobs WHERE id=?", (delivered.id,)
        ).fetchone()[0] == "closed"

