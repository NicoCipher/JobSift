from __future__ import annotations

import json
import sqlite3
from collections import Counter

import pytest

from validation.workday_production_v1 import evaluate


def test_frozen_workday_cohort_and_provenance_are_enforced(tmp_path, monkeypatch) -> None:
    cohort = evaluate.validation_inputs()

    assert len(cohort) == 15
    assert cohort[0]["host"] == "bigcommerce.wd12.myworkdayjobs.com"
    assert all(row["company_historical_label"].casefold() != "nvidia" for row in cohort)

    monkeypatch.setattr(evaluate, "BRIEF", tmp_path / "different-brief.json")
    (tmp_path / "different-brief.json").write_text("{}")
    with pytest.raises(AssertionError):
        evaluate.validation_inputs()


@pytest.mark.parametrize(
    ("attribute", "value"),
    [("MATCHER_VERSION", "different-version"), ("DEDUPE_VERSION", "different-version")],
)
def test_workday_validation_versions_remain_enforced(attribute, value, monkeypatch) -> None:
    monkeypatch.setattr(evaluate, attribute, value)

    with pytest.raises(AssertionError):
        evaluate.validation_inputs()


def test_production_summary_excludes_nvidia_from_the_frozen_cohort() -> None:
    boards = [
        {
            "company": "Commerce",
            "board": "commerce.example:commerce:Commerce",
            "pipeline": {"status": "success"},
            "collector_counts": {"paths_discovered": 2, "normalized": 2, "quarantined": 0},
            "errors": [],
            "delivered": 1,
        },
        {
            "company": "CH robison",
            "board": "ch.example:ch:ch",
            "pipeline": {"status": "partial"},
            "collector_counts": {"paths_discovered": 1, "normalized": 0, "quarantined": 1},
            "errors": ["detail /job/Test: ValueError"],
            "delivered": 0,
        },
    ]

    summary = evaluate.production_summary(boards, Counter({"reject": 2}), completed_at="fixed")

    assert summary["boards"] == 2
    assert summary["totals"] == {
        "discovered": 3,
        "normalized": 2,
        "quarantined": 1,
        "errors": 1,
        "delivered": 1,
    }
    assert summary["statuses"] == {"success": 1, "partial": 1}
    assert summary["partial_boards"][0]["company"] == "CH robison"
    assert summary["nvidia_cap_recovery"]["separate_from_production_cohort"] is True
    assert summary["nvidia_cap_recovery"]["unique_job_req_ids"] == 2695


def test_regenerate_summary_uses_preserved_artifacts_without_collection(
    tmp_path, monkeypatch
) -> None:
    board = {
        "company": "Commerce",
        "board": "commerce.example:commerce:Commerce",
        "pipeline": {"status": "success"},
        "collector_counts": {"paths_discovered": 1, "normalized": 1, "quarantined": 0},
        "errors": [],
        "delivered": 0,
    }
    (tmp_path / "boards.json").write_text(json.dumps([board]))
    directory = tmp_path / "commerce"
    directory.mkdir()
    with sqlite3.connect(directory / "jobs.sqlite3") as connection:
        connection.execute("CREATE TABLE job_matches (decision TEXT)")
        connection.execute("INSERT INTO job_matches VALUES ('reject')")
    monkeypatch.setattr(evaluate, "WorkdayCollector", lambda: pytest.fail("must not collect"))

    summary = evaluate.regenerate_summary(tmp_path)

    assert summary["decisions"] == {"reject": 1}
    assert json.loads((tmp_path / "summary.json").read_text())["boards"] == 1
