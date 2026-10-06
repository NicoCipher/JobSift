from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from job_scout import sourcing_plan
from job_scout.domain.models import Job, JobMatch
from job_scout.normalization.core import content_fingerprint
from job_scout.storage.daily_batches import DailyBatchStore
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


def test_recent_inventory_retains_needs_review_candidate_ids(tmp_path, monkeypatch):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    confirmed = posting("confirmed", "Software Engineer")
    reviewable = posting("reviewable", "Backend Engineer")
    rejected = posting("rejected", "Sales Manager")
    repo.upsert_jobs([confirmed, reviewable, rejected])
    brief = SimpleNamespace(client_id="client-a")

    decisions = {
        "confirmed": "strong_match",
        "reviewable": "needs_review",
        "rejected": "reject",
    }

    monkeypatch.setattr(
        sourcing_plan,
        "match_job",
        lambda job, _brief: JobMatch(
            job_id=job.id,
            client_id="client-a",
            decision=decisions[job.id],
            evaluated_at=datetime.now(UTC),
            matcher_version="test",
        ),
    )

    report, candidate_ids = sourcing_plan.evaluate_recent_inventory(
        repository=repo,
        brief=brief,
        retention_hours=72,
    )

    assert report.total_evaluated == 3
    assert report.total_matched == 1
    assert report.total_needs_review == 1
    assert report.total_rejected == 1
    assert candidate_ids == ("confirmed", "reviewable")


def test_recent_inventory_rejects_old_posting_reverified_now(tmp_path, monkeypatch):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    now = datetime.now(UTC)
    old = now - timedelta(hours=80)
    stale = posting("stale", "Software Engineer").model_copy(
        update={
            "posted_at": old,
            "discovered_at": old,
            "last_seen_at": now,
        }
    )
    repo.upsert_job(stale)

    with repo.connect() as connection:
        last_verified_at = datetime.fromisoformat(
            connection.execute(
                "SELECT last_verified_at FROM jobs WHERE id=?", (stale.id,)
            ).fetchone()[0]
        )
    assert last_verified_at >= now - timedelta(hours=72)

    monkeypatch.setattr(
        sourcing_plan,
        "match_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("expired payload must not reach matching")
        ),
    )

    report, candidate_ids = sourcing_plan.evaluate_recent_inventory(
        repository=repo,
        brief=SimpleNamespace(client_id="client-a"),
        retention_hours=72,
        evaluated_at=now,
    )

    assert report.total_evaluated == 0
    assert report.total_matched == 0
    assert candidate_ids == ()

def test_recent_inventory_persists_revision_scoped_matches(tmp_path, monkeypatch):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    eligible = posting("eligible", "Software Engineer")
    repo.upsert_job(eligible)
    brief = SimpleNamespace(client_id="client-a")

    monkeypatch.setattr(
        sourcing_plan,
        "match_job",
        lambda job, _brief: JobMatch(
            job_id=job.id,
            client_id="client-a",
            decision="strong_match",
            evaluated_at=datetime.now(UTC),
            matcher_version="test",
        ),
    )

    _report, candidate_ids = sourcing_plan.evaluate_recent_inventory(
        repository=repo,
        brief=brief,
        retention_hours=72,
        match_scope_id="brief-revision-a",
    )

    with repo.connect() as connection:
        row = connection.execute(
            "SELECT decision FROM scoped_job_matches "
            "WHERE job_id=? AND client_id=? AND match_scope_id=?",
            ("eligible", "client-a", "brief-revision-a"),
        ).fetchone()

    assert row is not None
    assert row["decision"] == "strong_match"
    assert DailyBatchStore(repo).evidence_digest(
        "client-a",
        candidate_ids,
        match_scope_id="brief-revision-a",
    )

