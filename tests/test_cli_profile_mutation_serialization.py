import pytest

from job_scout import cli


class FakeRepository:
    def __init__(self, remote_url: str) -> None:
        self.remote_url = remote_url


def test_local_profile_mutation_does_not_require_actions_queue(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_WORKFLOW", raising=False)
    cli._require_serialized_profile_mutation(FakeRepository(""), "pause")


def test_remote_profile_mutation_rejects_direct_cli(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_WORKFLOW", raising=False)
    with pytest.raises(RuntimeError, match="must run through"):
        cli._require_serialized_profile_mutation(
            FakeRepository("libsql://jobsift.example"),
            "pause",
        )


@pytest.mark.parametrize(
    "workflow",
    ["Client Delivery Control", "Configure Client Delivery Profile"],
)
def test_remote_profile_mutation_accepts_only_serialized_workflows(monkeypatch, workflow):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_WORKFLOW", workflow)
    cli._require_serialized_profile_mutation(
        FakeRepository("libsql://jobsift.example"),
        "pause",
    )


def test_remote_profile_mutation_rejects_unrelated_actions_workflow(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_WORKFLOW", "Refresh Live Job Inventory")
    with pytest.raises(RuntimeError, match="must run through"):
        cli._require_serialized_profile_mutation(
            FakeRepository("libsql://jobsift.example"),
            "pause",
        )
