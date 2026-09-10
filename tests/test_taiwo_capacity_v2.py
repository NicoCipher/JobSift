from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from job_scout.domain.models import Job
from job_scout.history import HistoricalRecord
from job_scout.matching.matcher import match_job
from job_scout.storage.sqlite import SQLiteRepository
from validation.taiwo_sourcing_capacity_v2 import run


def job(number="1", title="Technical Support Specialist"):
    return Job(
        id=number,
        source="greenhouse",
        source_board_id="acme",
        source_job_id=number,
        title=title,
        company="Acme",
        description_text="Troubleshooting Linux networking. " * 50,
        job_url=f"https://example.test/jobs/{number}",
        canonical_url=f"https://example.test/jobs/{number}",
        country="United States",
        remote_status="remote",
        content_fingerprint=f"content-{number}",
    )


def test_brief_bytes_vocabulary_and_all_non_title_fields():
    old, new = run.briefs()
    assert run.file_sha(run.V1) == run.V1_SHA
    assert new.target_roles == old.target_roles + [
        "Technical Support Specialist",
        "IT Support",
        "IT Field Support Specialist",
        "Technical Support Analyst",
        "IT Systems Administrator",
        "Desktop Support Specialist",
        "Analyst Service Desk",
    ]
    # Compare raw JSON too, including notes and the schema discriminator.
    a, b = json.loads(run.V1.read_text()), json.loads(run.V2.read_text())
    a.pop("target_roles")
    b.pop("target_roles")
    assert a == b
    assert old.client_id == new.client_id == run.CLIENT
    assert old.brief_version == new.brief_version == "operator-style-sourcing-brief-v1"


def test_production_matcher_recomputes_under_same_identity():
    old, new = run.briefs()
    assert match_job(job(), old).decision.value == "reject"
    assert match_job(job(), new).decision.value == "strong_match"
    assert match_job(job(), new).client_id == old.client_id
    for title in (
        "Customer Support Specialist",
        "Product Support Specialist",
        "Systems Administrator",
    ):
        assert match_job(job(title=title), new).decision.value == "reject"


@pytest.mark.parametrize("status", ["applied", "not_applied", "unknown"])
def test_v2_keeps_historical_suppression_and_delivery_identity(tmp_path, status):
    old, new = run.briefs()
    repo = SQLiteRepository(tmp_path / "fixture.sqlite3")
    first, second = job(), job("2", "IT Support Technician")
    repo.upsert_job(first)
    repo.upsert_job(second)
    repo.import_historical_records(
        client_id=old.client_id,
        workbook_sha256="f" * 64,
        records=[
            HistoricalRecord(
                original_url=str(first.canonical_url),
                normalized_url=str(first.canonical_url),
                source=first.source,
                source_board_id=first.source_board_id,
                source_job_id=first.source_job_id,
                title=first.title,
                company=first.company,
                operator_status=status,
                source_sheet="History",
                source_row=1,
            )
        ],
    )
    repo.mark_exported(second.id, old.client_id, run.DESTINATION)
    candidates = [first, second]
    assert all(match_job(j, new).decision.value in run.ELIGIBLE for j in candidates)
    assert repo.select_deliveries(candidates, new.client_id, run.DESTINATION) == []
    with run.readonly(Path(repo.path)) as connection:
        result = run.delivery_projection(candidates, connection, new.client_id)
    assert result["historical_identity"] == 1
    assert result["already_delivered_postings"] == 1
    assert result["fresh_groups"] == 0
    # Changing either client or destination is not safe for preserving all delivery state.
    assert len(repo.select_deliveries(candidates, "different-client", run.DESTINATION)) == 2
    assert repo.select_deliveries(candidates, new.client_id, "different-destination") == [second]


def test_projection_matches_production_url_fallback_and_grouping(tmp_path):
    repo = SQLiteRepository(tmp_path / "fixture.sqlite3")
    first, second, third = job(), job("2"), job("3", "IT Support")
    second.canonical_url = first.canonical_url
    repo.upsert_job(first)
    repo.upsert_job(second)
    repo.upsert_job(third)
    repo.import_historical_records(
        client_id=run.CLIENT,
        workbook_sha256="a" * 64,
        records=[
            HistoricalRecord(
                original_url=str(third.canonical_url),
                normalized_url=str(third.canonical_url),
                source=None,
                source_board_id=None,
                source_job_id=None,
                title="unrelated title",
                company="unrelated company",
                operator_status="unknown",
                source_sheet="History",
                source_row=1,
            )
        ],
    )
    candidates = [third, second, first]
    expected = repo.select_deliveries(candidates, run.CLIENT, run.DESTINATION)
    with run.readonly(Path(repo.path)) as connection:
        result = run.delivery_projection(candidates, connection)
    assert set(result["selected_groups"].values()) == {j.id for j in expected}
    assert result["fresh_groups"] == 1
    assert result["historical_url"] == 1
    assert result["practical_duplicate_postings_collapsed"] == 1


def test_readonly_rejects_writes_and_missing_inputs(tmp_path):
    path = tmp_path / "fixture.sqlite3"
    repo = SQLiteRepository(path)
    repo.upsert_job(job())
    before = run.file_sha(path)
    with (
        run.readonly(path) as connection,
        pytest.raises(sqlite3.OperationalError, match="readonly"),
    ):
        connection.execute("DELETE FROM jobs")
    assert run.file_sha(path) == before
    with pytest.raises(sqlite3.OperationalError), run.readonly(tmp_path / "missing.sqlite3"):
        pass
    assert not (tmp_path / "missing.sqlite3").exists()


def test_handoff_preserves_order_and_distinguishes_attempt_from_acquisition():
    targets = [{"target_identity": key} for key in ("success", "failed", "unattempted")]
    rows = [
        {"target_identity": "failed", "state": "completed", "collection_status": "parse_failure"},
        {"target_identity": "success", "state": "completed", "collection_status": "success"},
    ]
    result = run.handoff_targets(targets, rows)
    assert [r["target_identity"] for r in result] == [r["target_identity"] for r in targets]
    assert [r["v2_disposition"] for r in result] == [
        "reuse_successful_acquisition",
        "explicit_review_before_future_acquisition",
        "not_yet_attempted",
    ]
    assert result[1]["v1_state"]["collection_status"] == "parse_failure"


def test_frozen_outputs_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "OUTPUT", tmp_path)
    run.persist_outputs({"manifest.json": {"frozen": True}})
    before = (tmp_path / "manifest.json").read_bytes()
    run.persist_outputs({"manifest.json": {"frozen": True}})
    with pytest.raises(ValueError, match="refuse to overwrite"):
        run.persist_outputs({"new.json": {}, "manifest.json": {"frozen": False}})
    assert (tmp_path / "manifest.json").read_bytes() == before
    assert not (tmp_path / "new.json").exists()


def test_frozen_inputs_reject_drift(monkeypatch):
    monkeypatch.setattr(run, "input_fingerprints", lambda: {"input": "changed"})
    with pytest.raises(ValueError, match="fingerprints changed"):
        run.verify_input_fingerprints({"input": "frozen"})


def test_full_reproduction_does_not_require_stage_b_summary(tmp_path, monkeypatch):
    """An unavailable Stage B summary cannot affect the actual replay path."""
    unavailable = run.V1_ROOT / "stage_b" / "summary.json"
    original_read_text = Path.read_text

    def read_text_without_stage_b_summary(path, *args, **kwargs):
        if path == unavailable:
            raise AssertionError("Stage B summary must not be read")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text_without_stage_b_summary)
    output = tmp_path / "v2"
    output.mkdir()
    monkeypatch.setattr(run, "OUTPUT", output)

    run.main()

    summary = json.loads((run.OUTPUT / "summary.json").read_text())
    assert summary["capacity"]["equivalent_v2_fresh_baseline"] == 56
    assert summary["matching"]["v2"]["delivery_eligible"] == 62
    assert (
        "validation/taiwo_sourcing_capacity_v1/stage_b/summary.json"
        not in json.loads((run.OUTPUT / "manifest.json").read_text())["inputs_sha256"]
    )


def test_frozen_manifest_and_production_matcher_integrity():
    manifest = json.loads((run.OUTPUT / "manifest.json").read_text())
    claimed = manifest.pop("manifest_sha256")
    assert run.sha_value(manifest) == claimed
    assert manifest["brief_revision"] == run.REVISION
    assert manifest["operator_client_id"] == run.CLIENT
    assert manifest["cumulative_destination"] == run.DESTINATION
    assert manifest["v1_brief_sha256"] == run.file_sha(run.V1) == run.V1_SHA
    assert manifest["v2_brief_sha256"] == run.file_sha(run.V2)
    assert run.file_sha(run.ROOT / "job_scout/matching/matcher.py") == (
        "40be2da3a73dcaffa44e159a5e1ca460c1b1d2397bfc2c3987d1bcd0492c5446"
    )
    # These checks require only committed code/config, never the real runtime DBs.
    for path, expected in manifest["inputs_sha256"].items():
        if path.startswith("job_scout/") or path.endswith("capacity_v2/run.py"):
            assert run.file_sha(run.ROOT / path) == expected


def test_saved_baseline_reconciles_and_mismatch_is_rejected():
    summary = json.loads((run.OUTPUT / "summary.json").read_text())
    manifest = json.loads((run.OUTPUT / "manifest.json").read_text())
    assert summary["manifest_sha256"] == manifest["manifest_sha256"]
    run.verify_results(summary)
    assert summary["capacity"]["equivalent_v2_fresh_baseline"] == 56
    assert summary["capacity"]["existing_workday_v2_incremental"] == 0
    assert len(summary["inherited_cumulative_markers"]) == 44
    assert summary["historical_rows"] == 11107
    incremental = summary["incremental_delivery_projection"]
    assert incremental["historical_identity"] == incremental["historical_url"] == 0
    assert incremental["already_delivered_postings"] == 0
    assert incremental["practical_duplicate_postings_collapsed"] == 2
    assert len(incremental["selected_groups"]) == 12
    assert len(summary["incremental_eligible_evidence"]) == 14
    summary["matching"]["v2"]["delivery_eligible"] += 1
    with pytest.raises(ValueError, match="reproduction failed"):
        run.verify_results(summary)


def test_saved_handoff_preserves_frozen_targets_and_failed_evidence():
    handoff = json.loads((run.OUTPUT / "workday_handoff.json").read_text())
    original = json.loads((run.V1_ROOT / "stage_b/manifest.json").read_text())
    assert handoff["network_authorized"] is False
    assert len(handoff["targets"]) == len(original["targets"]) == 536
    assert sum(t["acquired_postings"] for t in handoff["targets"]) == 337
    for current, prior in zip(handoff["targets"], original["targets"], strict=True):
        assert {key: current[key] for key in prior} == prior
    penn = next(t for t in handoff["targets"] if "pennmutual" in t["target_identity"])
    assert penn["v1_state"]["collection_status"] == "parse_failure"
    assert penn["v2_disposition"] == "explicit_review_before_future_acquisition"
    assert penn["acquired_postings"] == 0
