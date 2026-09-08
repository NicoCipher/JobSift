from __future__ import annotations

import json
from pathlib import Path

from validation.lever_contract_v1 import audit


def test_frozen_cohort_manifest_hash_is_valid() -> None:
    cohort = json.loads(audit.COHORT_PATH.read_text(encoding="utf-8"))
    manifest_sha256 = cohort.pop("manifest_sha256")

    assert manifest_sha256 == audit.sha256(cohort)
    assert len(cohort["targets"]) == 12


def test_checkpoint_round_trip_rejects_tampering(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(audit, "CHECKPOINT_DIR", tmp_path)
    path = audit.checkpoint_path("global", "example")
    payload = {
        "attempts": [],
        "cohort_manifest_sha256": "manifest",
        "current_outcome": "success",
        "host": "jobs.lever.co",
        "instance": "global",
        "site": "example",
    }

    audit.write_checkpoint(path, payload)

    assert audit.read_checkpoint(path, "manifest")["site"] == "example"

    checkpoint = json.loads(path.read_text(encoding="utf-8"))
    checkpoint["site"] = "changed"
    path.write_text(json.dumps(checkpoint), encoding="utf-8")

    assert audit.read_checkpoint(path, "manifest") is None


def test_completed_checkpoints_reuse_only_verified_terminal_results(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(audit, "CHECKPOINT_DIR", tmp_path)
    cohort = {
        "manifest_sha256": "manifest",
        "targets": [
            {"instance": "global", "site": "complete"},
            {"instance": "global", "site": "retry"},
        ],
    }
    for site, outcome in (("complete", "success"), ("retry", "request_timeout")):
        audit.write_checkpoint(
            audit.checkpoint_path("global", site),
            {
                "attempts": [],
                "cohort_manifest_sha256": "manifest",
                "current_outcome": outcome,
                "host": "jobs.lever.co",
                "instance": "global",
                "site": site,
            },
        )

    completed = audit.completed_checkpoints(cohort)

    assert set(completed) == {("global", "complete")}


def test_site_outcomes_keep_operational_failures_distinct() -> None:
    board = {
        "board_status": "partial",
        "request_failures": ["ReadTimeout: timed out"],
        "rows_retrieved": 0,
    }

    assert audit.classify_site_outcome(board) == "request_timeout"

    board["request_failures"] = []

    assert audit.classify_site_outcome(board) == "empty_valid_board"
