import csv
import json
from pathlib import Path

import httpx

from job_scout.collectors.greenhouse import GreenhouseCollector
from job_scout.domain.models import CandidateProfile, SourceTarget
from job_scout.export.csv_exporter import CSV_COLUMNS
from job_scout.orchestration.pipeline import run_pipeline
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
