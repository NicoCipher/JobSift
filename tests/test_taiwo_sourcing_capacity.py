from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import pytest

from job_scout.domain.models import CollectionResult, Job, SearchBrief
from job_scout.history import HistoricalRecord
from job_scout.storage.sqlite import SQLiteRepository
from validation.taiwo_sourcing_capacity_v1 import run as stage_a


def posting(number: str) -> Job:
    return Job(
        id=f"job-{number}",
        source="greenhouse",
        source_board_id="acme",
        source_job_id=number,
        title="Support Engineer",
        company="Acme",
        description_text="Troubleshoot customer systems and investigate Linux networking faults. "
        * 12,
        job_url=f"https://example.test/jobs/{number}",
        canonical_url=f"https://example.test/jobs/{number}",
        country="United States",
        remote_status="remote",
        department="Support",
        content_fingerprint=f"fingerprint-{number}",
    )


def brief() -> SearchBrief:
    return SearchBrief.model_validate_json(stage_a.BRIEF_PATH.read_text())


def target() -> dict[str, object]:
    return {
        "source": "greenhouse",
        "target_identity": "greenhouse:acme",
        "coordinates": {"board": "acme"},
        "company_hint": "Acme",
        "historical_occurrence_count": 2,
        "health_current_inventory": 3,
    }


def manifest() -> dict[str, object]:
    value = {
        "manifest_sha256": "manifest",
        "threshold": 250,
        "targets": [target()],
    }
    return value


def test_manifest_is_active_only_non_workday_and_deterministically_ordered():
    value = stage_a.load_manifest()
    assert value["target_counts"] == stage_a.EXPECTED_COUNTS
    assert value["health_inventory"] == stage_a.EXPECTED_INVENTORY
    assert len(value["targets"]) == 1508
    assert {item["source"] for item in value["targets"]} == set(stage_a.SOURCES)
    assert all(item["source"] != "workday" for item in value["targets"])
    assert value["targets"] == sorted(
        value["targets"],
        key=lambda item: (
            -item["historical_occurrence_count"],
            -item["health_current_inventory"],
            item["target_identity"],
        ),
    )


def test_checkpoint_is_manifest_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(stage_a, "CHECKPOINTS", tmp_path / "checkpoints")
    item, frozen = target(), manifest()
    stage_a.write_json(
        stage_a.checkpoint_path(item),
        {
            "manifest_sha256": "different",
            "target_identity": "greenhouse:acme",
            "source": "greenhouse",
        },
    )
    with pytest.raises(ValueError, match="mismatched"):
        stage_a.completed(item, frozen)


def test_totals_track_historical_and_dedupe_metrics_and_threshold():
    values = [
        {
            "source": "greenhouse",
            "collection_status": "success",
            "received": 3,
            "quarantined": 0,
            "lifecycle": {"new": 3},
            "matcher_decisions": {"strong_match": 3},
            "delivery_eligible_matched": 3,
            "historical_identity": 1,
            "historical_url": 1,
            "nonhistorical_matched": 1,
            "practical_duplicate_groups_collapsed": 0,
            "already_delivered_groups": 0,
            "fresh_unique_deliveries": 250,
            "deliveries": [{"decision": "strong_match"}] * 250,
        }
    ]
    frozen = {"manifest_sha256": "manifest", "threshold": 250, "targets": [target()]}
    total = stage_a._totals(values, frozen)
    assert total["historical_suppression"] == {
        "exact_source_identity": 1,
        "normalized_url_fallback": 1,
        "total": 2,
    }
    assert total["fresh_unique_deliveries"] == 250
    assert total["stop_reason"] == "fresh_delivery_threshold_reached"


class FrozenCollector:
    def __init__(self, jobs: list[Job]) -> None:
        self.jobs = jobs

    def collect(self, _target) -> CollectionResult:
        return CollectionResult(
            source="greenhouse", target=_target, jobs=self.jobs, status="success"
        )


def test_execute_target_uses_actual_history_and_delivery_pipeline(tmp_path, monkeypatch):
    repository = SQLiteRepository(tmp_path / "replay.sqlite3")
    old, fresh = posting("1"), posting("2")
    historical = HistoricalRecord(
        original_url=str(old.canonical_url),
        normalized_url=str(old.canonical_url),
        source="greenhouse",
        source_board_id="acme",
        source_job_id="1",
        title=old.title,
        company=old.company,
        operator_status="unknown",
        source_sheet="History",
        source_row=2,
    )
    repository.import_historical_records(
        client_id=brief().client_id, workbook_sha256="a" * 64, records=[historical]
    )
    monkeypatch.setattr(stage_a, "_collector", lambda _source: FrozenCollector([old, fresh]))
    value = stage_a.execute_target(
        target=target(),
        manifest=manifest(),
        repository=repository,
        brief=brief(),
        destination=tmp_path / "deliveries.csv",
    )
    assert value["received"] == 2
    assert value["historical_identity"] == 1
    assert value["fresh_unique_deliveries"] == 1
    with repository.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM job_matches").fetchone()[0] == 2


def test_lock_recovers_stale_file_and_rejects_active_file(tmp_path, monkeypatch):
    monkeypatch.setattr(stage_a, "RUNTIME", tmp_path / "runtime")
    monkeypatch.setattr(stage_a, "LOCK", tmp_path / "runtime" / "run.lock")
    stage_a.LOCK.parent.mkdir(parents=True)
    stage_a.LOCK.write_text("not-json")
    with stage_a.run_lock():
        assert stage_a.LOCK.exists()
    stage_a.LOCK.write_text(
        json.dumps({"pid": os.getpid(), "started_at": datetime.now(UTC).isoformat()})
    )
    with pytest.raises(RuntimeError, match="already running"), stage_a.run_lock():
        pass


def test_status_does_not_execute_targets(monkeypatch):
    monkeypatch.setattr(stage_a, "execute_target", lambda **_kwargs: pytest.fail("network work"))
    value = stage_a.status()
    assert value["completed_targets"] + value["remaining_targets"] == 1508


def test_runtime_paths_are_never_production_paths():
    for path in (stage_a.DATABASE, stage_a.DELIVERIES, stage_a.QUALITY_AUDIT):
        assert stage_a.RUNTIME in path.parents
