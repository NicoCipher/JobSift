import csv
import json
from copy import deepcopy
from pathlib import Path

import httpx

from job_scout.collectors.greenhouse import GreenhouseCollector
from job_scout.domain.models import CandidateProfile, RoleTargets, Skills, SourceTarget
from job_scout.export.csv_exporter import CSV_COLUMNS
from job_scout.orchestration.pipeline import run_pipeline
from job_scout.storage.sqlite import SQLiteRepository

FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_jobs.json"
TARGET = SourceTarget(board_id="acme", company="Acme")


def make_collector(payload: dict | None = None, status: int = 200) -> GreenhouseCollector:
    body = deepcopy(payload) if payload is not None else json.loads(FIXTURE.read_text())
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, json=body, request=request)
    )
    return GreenhouseCollector(httpx.Client(transport=transport))


def make_profile(client_id: str, required: list[str] | None = None) -> CandidateProfile:
    return CandidateProfile(
        client_id=client_id,
        target_roles=RoleTargets(include=["Backend Engineer"]),
        skills=Skills(required=required or [], preferred=["Python"]),
    )


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == CSV_COLUMNS
        return list(reader)


def test_retry_preserves_rows_and_does_not_duplicate(tmp_path) -> None:
    repo = SQLiteRepository(tmp_path / "jobs.db")
    output = tmp_path / "client.csv"
    profile = make_profile("client-a")
    first = run_pipeline(
        collector=make_collector(), target=TARGET, profile=profile, repository=repo, csv_path=output
    )
    first_rows = rows(output)
    second = run_pipeline(
        collector=make_collector(), target=TARGET, profile=profile, repository=repo, csv_path=output
    )
    assert first.exported == 1
    assert second.exported == 0
    assert rows(output) == first_rows
    assert len(rows(output)) == 1


def test_failed_collection_does_not_erase_existing_csv(tmp_path) -> None:
    repo = SQLiteRepository(tmp_path / "jobs.db")
    output = tmp_path / "client.csv"
    profile = make_profile("client-a")
    run_pipeline(
        collector=make_collector(), target=TARGET, profile=profile, repository=repo, csv_path=output
    )
    before = output.read_bytes()
    failed = run_pipeline(
        collector=make_collector(status=503),
        target=TARGET,
        profile=profile,
        repository=repo,
        csv_path=output,
    )
    assert failed.status == "provider_error"
    assert output.read_bytes() == before


def test_partial_collection_does_not_erase_existing_csv(tmp_path) -> None:
    repo = SQLiteRepository(tmp_path / "jobs.db")
    output = tmp_path / "client.csv"
    profile = make_profile("client-a")
    payload = json.loads(FIXTURE.read_text())
    run_pipeline(
        collector=make_collector(payload),
        target=TARGET,
        profile=profile,
        repository=repo,
        csv_path=output,
    )
    before = output.read_bytes()
    payload["jobs"].append({"id": 999, "title": ""})
    partial = run_pipeline(
        collector=make_collector(payload),
        target=TARGET,
        profile=profile,
        repository=repo,
        csv_path=output,
    )
    assert partial.status == "partial"
    assert partial.exported == 0
    assert output.read_bytes() == before


def test_delivery_state_is_client_and_destination_specific(tmp_path) -> None:
    repo = SQLiteRepository(tmp_path / "jobs.db")
    output_a = tmp_path / "client-a.csv"
    output_b = tmp_path / "client-b.csv"
    first_a = run_pipeline(
        collector=make_collector(),
        target=TARGET,
        profile=make_profile("client-a"),
        repository=repo,
        csv_path=output_a,
    )
    first_b = run_pipeline(
        collector=make_collector(),
        target=TARGET,
        profile=make_profile("client-b"),
        repository=repo,
        csv_path=output_b,
    )
    rerun_a = run_pipeline(
        collector=make_collector(),
        target=TARGET,
        profile=make_profile("client-a"),
        repository=repo,
        csv_path=output_a,
    )
    rerun_b = run_pipeline(
        collector=make_collector(),
        target=TARGET,
        profile=make_profile("client-b"),
        repository=repo,
        csv_path=output_b,
    )
    assert (first_a.exported, first_b.exported) == (1, 1)
    assert (rerun_a.exported, rerun_b.exported) == (0, 0)
    assert len(rows(output_a)) == len(rows(output_b)) == 1
    with repo.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


def test_rejected_changed_job_can_become_deliverable_without_new_identity(tmp_path) -> None:
    repo = SQLiteRepository(tmp_path / "jobs.db")
    output = tmp_path / "client.csv"
    profile = make_profile("client-a", required=["Python"])
    initial = json.loads(FIXTURE.read_text())
    initial["jobs"] = [initial["jobs"][0]]
    initial["jobs"][0]["content"] = "&lt;p&gt;Build services with Ruby.&lt;/p&gt;"
    rejected = run_pipeline(
        collector=make_collector(initial),
        target=TARGET,
        profile=profile,
        repository=repo,
        csv_path=output,
    )
    changed = deepcopy(initial)
    changed["jobs"][0]["content"] = "&lt;p&gt;Build services with Python.&lt;/p&gt;"
    accepted = run_pipeline(
        collector=make_collector(changed),
        target=TARGET,
        profile=profile,
        repository=repo,
        csv_path=output,
    )
    assert rejected.exported == 0
    assert accepted.changed == 1 and accepted.exported == 1
    assert len(rows(output)) == 1
    with repo.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_changed_already_exported_job_does_not_append_duplicate(tmp_path) -> None:
    repo = SQLiteRepository(tmp_path / "jobs.db")
    output = tmp_path / "client.csv"
    profile = make_profile("client-a")
    initial = json.loads(FIXTURE.read_text())
    run_pipeline(
        collector=make_collector(initial),
        target=TARGET,
        profile=profile,
        repository=repo,
        csv_path=output,
    )
    changed = deepcopy(initial)
    changed["jobs"][0]["content"] += "&lt;p&gt;Updated requirement.&lt;/p&gt;"
    result = run_pipeline(
        collector=make_collector(changed),
        target=TARGET,
        profile=profile,
        repository=repo,
        csv_path=output,
    )
    assert result.changed == 1
    assert result.exported == 0
    assert len(rows(output)) == 1
