from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from job_scout import sourcing_plan
from job_scout.domain.models import Job, JobMatch
from job_scout.normalization.core import content_fingerprint
from job_scout.storage.sqlite import SQLiteRepository


def posting(job_id: str, title: str) -> Job:
    now = datetime.now(UTC)
    description = f"{title} role"
    return Job(
        id=job_id,
        source="greenhouse",
        source_job_id=job_id,
        source_board_id="acme",
        title=title,
        company="Acme",
        employer_id="acme",
        description_text=description,
        job_url=f"https://example.com/{job_id}",
        canonical_url=f"https://example.com/{job_id}",
        posted_at=now,
        discovered_at=now,
        last_seen_at=now,
        content_fingerprint=content_fingerprint(
            title=title,
            description=description,
            location=None,
            employment_type=None,
        ),
    )


def test_recent_inventory_returns_only_delivery_eligible_candidate_ids(
    tmp_path, monkeypatch
):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    eligible = posting("eligible", "Software Engineer")
    rejected = posting("rejected", "Sales Manager")
    repo.upsert_jobs([eligible, rejected])
    brief = SimpleNamespace(client_id="client-a")

    def fake_match(job, _brief):
        return JobMatch(
            job_id=job.id,
            client_id="client-a",
            decision="strong_match" if job.id == "eligible" else "reject",
            evaluated_at=datetime.now(UTC),
            matcher_version="test",
        )

    monkeypatch.setattr(sourcing_plan, "match_job", fake_match)

    report, candidate_ids = sourcing_plan.evaluate_recent_inventory(
        repository=repo,
        brief=brief,
        retention_hours=72,
    )

    assert report.total_evaluated == 2
    assert report.total_matched == 1
    assert report.total_rejected == 1
    assert candidate_ids == ("eligible",)
