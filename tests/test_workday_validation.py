from __future__ import annotations

import pytest

from validation.workday_production_v1 import evaluate


def test_frozen_workday_cohort_and_provenance_are_enforced(tmp_path, monkeypatch) -> None:
    cohort = evaluate.validation_inputs()

    assert len(cohort) == 15
    assert cohort[0]["host"] == "bigcommerce.wd12.myworkdayjobs.com"

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
