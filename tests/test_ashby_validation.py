import hashlib
import json

import pytest

from validation.ashby_production_v1 import evaluate


def test_offline_requires_frozen_raw_payloads(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluate, "RAW", tmp_path / "raw")

    with pytest.raises(FileNotFoundError):
        evaluate.validation_inputs("offline")


def test_live_can_begin_without_frozen_raw_payloads(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluate, "RAW", tmp_path / "raw")

    cohort = evaluate.validation_inputs("live")

    assert tuple(row["board_token"] for row in cohort) == evaluate.EXPECTED_BOARDS


def test_live_drift_is_explicitly_unavailable_without_frozen_raw(tmp_path, monkeypatch):
    row = {"board_token": "supabase", "sha256": "unused"}
    monkeypatch.setattr(evaluate, "RAW", tmp_path / "raw")

    _, previous, reason = evaluate.frozen_payload(row, required=False)
    drift = evaluate.historical_drift(previous, reason, {"jobs": [{"id": "current"}]}, "success")

    assert drift == {
        "historical_drift_available": False,
        "historical_drift_reason": "frozen raw payload not present in this environment",
        "previous_raw_count": None,
        "added_provider_ids": None,
        "removed_provider_ids": None,
    }


def test_live_drift_uses_hash_valid_frozen_payload(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir()
    content = json.dumps({"jobs": [{"id": "unchanged"}, {"id": "removed"}]}).encode()
    (raw / "supabase.json").write_bytes(content)
    row = {"board_token": "supabase", "sha256": hashlib.sha256(content).hexdigest()}
    monkeypatch.setattr(evaluate, "RAW", raw)

    _, previous, reason = evaluate.frozen_payload(row, required=False)
    drift = evaluate.historical_drift(
        previous,
        reason,
        {"jobs": [{"id": "unchanged"}, {"id": "added"}]},
        "success",
    )

    assert drift == {
        "historical_drift_available": True,
        "historical_drift_reason": None,
        "previous_raw_count": 2,
        "added_provider_ids": ["added"],
        "removed_provider_ids": ["removed"],
    }


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("MATCHER_VERSION", "different-version"),
        ("DEDUPE_VERSION", "different-version"),
    ],
)
def test_validation_versions_remain_enforced(attribute, value, monkeypatch):
    monkeypatch.setattr(evaluate, attribute, value)

    with pytest.raises(AssertionError):
        evaluate.validation_inputs("live")


def test_validation_brief_and_cohort_remain_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluate, "BRIEF", tmp_path / "different-brief.json")
    (tmp_path / "different-brief.json").write_text("{}")

    with pytest.raises(AssertionError):
        evaluate.validation_inputs("live")

    monkeypatch.setattr(
        evaluate, "BRIEF", evaluate.ROOT / "config/search_briefs/taiwo_operator_sourcing_v1.json"
    )
    monkeypatch.setattr(evaluate, "COHORT", tmp_path / "different-cohort.csv")
    (tmp_path / "different-cohort.csv").write_text("board_token\nnot-the-frozen-cohort\n")

    with pytest.raises(AssertionError):
        evaluate.validation_inputs("live")
