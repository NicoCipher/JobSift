import csv
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_scout.dedupe.resolver import delivery_keys, representative_key
from job_scout.domain.models import CollectionResult, Job, JobLifecycle, SearchBrief, SourceTarget
from job_scout.matching.matcher import match_job
from job_scout.orchestration.pipeline import run_pipeline
from job_scout.storage.sqlite import SQLiteRepository


def posting(number="1", **changes):
    data = {
        "id": f"posting-{number}",
        "source": "greenhouse",
        "source_board_id": "acme",
        "source_job_id": number,
        "title": "Support Engineer",
        "company": "Acme",
        "description_text": "Troubleshoot customer systems and investigate Linux networking faults. "
        * 12,
        "canonical_url": f"https://example.com/jobs/{number}",
        "job_url": f"https://example.com/jobs/{number}",
        "country": "United States",
        "remote_status": "remote",
        "department": "Support",
        "content_fingerprint": f"content-{number}",
    }
    data.update(changes)
    return Job(**data)


def test_exact_url_preserves_both_source_postings(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    first, second = posting(), posting("2", canonical_url="https://example.com/jobs/1")
    repo.upsert_job(first)
    repo.upsert_job(second)
    with repo.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


def test_exact_url_duplicate_match_has_no_orphan(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    first, second = posting(), posting("2", canonical_url="https://example.com/jobs/1")
    brief = SearchBrief(client_id="client", target_roles=["Support Engineer"])
    for job in (first, second):
        repo.upsert_job(job)
        repo.save_match(match_job(job, brief))
    with repo.connect() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


class FrozenCollector:
    def __init__(self, jobs):
        self.jobs = jobs

    def collect(self, target):
        return CollectionResult(source="fixture", target=target, jobs=self.jobs, status="success")


def run(repo, output, jobs, client="client"):
    return run_pipeline(
        collector=FrozenCollector(jobs),
        target=SourceTarget(board_id="acme", company="Acme"),
        profile=SearchBrief(client_id=client, target_roles=["Support Engineer"]),
        repository=repo,
        csv_path=output,
    )


def test_same_source_updates_posting_and_retains_delivery_history(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    job = posting()
    assert repo.upsert_job(job) is JobLifecycle.NEW
    repo.mark_exported(job.id, "client", "csv")
    updated = posting(
        id="different-incoming-id",
        content_fingerprint="changed",
        description_text="Entirely updated requirement",
    )
    assert repo.upsert_job(updated) is JobLifecycle.CHANGED
    assert updated.id == job.id
    assert repo.upsert_job(updated) is JobLifecycle.SEEN
    assert repo.is_exported(updated.id, "client", "csv")
    with repo.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"company": "Different Company"},
        {"country": "Canada"},
        {"description_text": "Diagnose databases and develop SQL query plans. " * 12},
        {"department": "Sales Engineering"},
        {"department": None},
        {"city": "Boston"},
        {"location_text": "Remote, United States, California only"},
        {"remote_status": "hybrid"},
        {"employment_type": "contract"},
    ],
)
def test_materially_different_postings_are_separate(tmp_path, changes):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    first, second = posting(), posting("2", **changes)
    for job in (first, second):
        repo.upsert_job(job)
    assert repo.delivery_group_id(first.id) != repo.delivery_group_id(second.id)


@pytest.mark.parametrize(
    "changes",
    [
        {"description_text": None},
        {"description_text": "Support customers"},
        {"country": None},
        {"remote_status": "unknown"},
    ],
)
def test_insufficient_evidence_stays_separate(tmp_path, changes):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    first, second = posting(**changes), posting("2", **changes)
    for job in (first, second):
        repo.upsert_job(job)
    assert repo.delivery_group_id(first.id) != repo.delivery_group_id(second.id)


def test_generic_careers_url_does_not_group_insufficient_evidence(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    jobs = [
        posting(n, description_text=None, canonical_url="https://example.com/careers")
        for n in ("1", "2")
    ]
    for job in jobs:
        repo.upsert_job(job)
    assert repo.delivery_group_id(jobs[0].id) != repo.delivery_group_id(jobs[1].id)


def test_exact_url_variants_group_and_preserve_provenance(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    first = posting(canonical_url="https://example.com/vacancy/1/?id=1&lang=en&utm_source=x")
    second = posting(
        "2",
        source="lever",
        description_text=None,
        canonical_url="https://example.com/vacancy/1?lang=en&id=1#apply",
    )
    for job in (first, second):
        repo.upsert_job(job)
    assert repo.delivery_group_id(first.id) == repo.delivery_group_id(second.id)
    with repo.connect() as c:
        assert len(c.execute("SELECT DISTINCT source FROM jobs").fetchall()) == 2


def test_meaningful_url_parameters_are_not_removed(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    jobs = [
        posting(n, description_text=None, canonical_url=f"https://example.com/job?id={n}")
        for n in ("1", "2")
    ]
    for job in jobs:
        repo.upsert_job(job)
    assert repo.delivery_group_id(jobs[0].id) != repo.delivery_group_id(jobs[1].id)


def test_cross_source_group_delivery_and_later_arrival(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    first = posting(location_text="Remote, US")
    second = posting("2", source="lever", location_text="Remote, United States")
    output = tmp_path / "jobs.csv"
    assert run(repo, output, [first]).exported == 1
    assert run(repo, output, [second]).exported == 0
    assert run(repo, output, [first, second]).exported == 0
    assert repo.delivery_group_id(first.id) == repo.delivery_group_id(second.id)
    # Client and destination are independently scoped.
    assert run(repo, output, [first, second], "other-client").exported == 1
    assert run(repo, tmp_path / "other.csv", [first, second]).exported == 1
    with output.open() as handle:
        assert len(list(csv.DictReader(handle))) == 2
    with repo.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM group_deliveries").fetchone()[0] == 3


def test_same_batch_representative_is_order_independent(tmp_path):
    first = posting(posted_at=datetime(2025, 1, 1, tzinfo=UTC))
    second = posting("2", posted_at=datetime(2026, 1, 1, tzinfo=UTC))
    representatives = []
    groups = []
    for index, jobs in enumerate(([first, second], [second, first])):
        repo = SQLiteRepository(tmp_path / f"{index}.db")
        result = run(repo, tmp_path / f"{index}.csv", jobs)
        assert result.matched == 2 and result.exported == 1
        with repo.connect() as c:
            representatives.append(c.execute("SELECT job_id FROM exports").fetchone()[0])
        groups.append(repo.delivery_group_id(first.id))
    assert representatives == [second.id, second.id]
    assert groups[0] == groups[1]


def test_representative_prefers_direct_apply_then_richer_evidence_then_dates():
    first = posting()
    direct = posting("2", apply_url="https://example.com/apply/2")
    rich = posting("3", description_text=first.description_text + "More evidence.")
    newer = posting("4", updated_at=datetime(2026, 1, 1, tzinfo=UTC))
    assert sorted([newer, rich, direct, first], key=representative_key) == [
        direct,
        rich,
        newer,
        first,
    ]
    assert representative_key(posting("1")) < representative_key(posting("2"))


def test_rejected_member_cannot_be_delivery_representative(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    first = posting(title="Sales Engineer", apply_url="https://example.com/apply/1")
    second = posting("2", canonical_url=str(first.canonical_url))
    result = run(repo, tmp_path / "jobs.csv", [first, second])
    assert result.exported == result.matched == result.rejected == 1
    with repo.connect() as c:
        assert c.execute("SELECT job_id FROM exports").fetchone()[0] == second.id


def test_observed_gitlab_pair_is_one_lead_with_two_postings(tmp_path):
    path = Path(__file__).parent / "fixtures/gitlab_government_support.json"
    jobs = [Job.model_validate(data) for data in json.loads(path.read_text())]
    assert jobs[0].description_text == jobs[1].description_text
    assert {job.source_job_id for job in jobs} == {"8707353002", "8628780002"}
    repo = SQLiteRepository(tmp_path / "jobs.db")
    result = run(repo, tmp_path / "jobs.csv", jobs)
    assert result.matched == 2 and result.exported == 1
    assert repo.delivery_group_id(jobs[0].id) == repo.delivery_group_id(jobs[1].id)
    with repo.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM job_matches").fetchone()[0] == 2
        selected = c.execute("SELECT job_id FROM group_deliveries").fetchone()[0]
        assert next(job for job in jobs if job.id == selected).source_job_id == "8707353002"


def legacy_database(path, jobs, orphan=False):
    schema = (Path(__file__).parent / "fixtures/sqlite_v5_schema.sql").read_text()
    with sqlite3.connect(path) as c:
        c.executescript(schema)
        for job in jobs:
            c.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job.id,
                    job.source,
                    job.source_job_id,
                    job.source_board_id,
                    str(job.canonical_url),
                    job.content_fingerprint,
                    job.discovered_at.isoformat(),
                    job.last_seen_at.isoformat(),
                    job.last_seen_at.isoformat(),
                    "seen",
                    job.model_dump_json(),
                ),
            )
            c.execute(
                "INSERT INTO exports VALUES (?,?,?,?)",
                (
                    job.id,
                    "client",
                    "csv",
                    "2026-01-01",
                ),
            )
    if orphan:
        with sqlite3.connect(path) as c:
            c.execute("INSERT INTO exports VALUES ('missing', 'client', 'csv', '2026-01-01')")


def test_migration_preserves_postings_matches_exports_and_is_idempotent(tmp_path):
    path = tmp_path / "old.db"
    jobs = [posting(), posting("2")]
    legacy_database(path, jobs)
    with sqlite3.connect(path) as c:
        c.execute(
            "INSERT INTO job_matches VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                jobs[0].id,
                "client",
                "possible_match",
                3,
                "[]",
                "[]",
                "2026-01-01",
                "deterministic-v5",
            ),
        )
        before = c.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    for _ in range(2):
        repo = SQLiteRepository(path)
        assert repo.is_exported(jobs[1].id, "client", "csv")
        with repo.connect() as c:
            assert [tuple(r) for r in c.execute("SELECT * FROM jobs ORDER BY id")] == before
            assert c.execute("SELECT COUNT(*) FROM exports").fetchone()[0] == 2
            assert c.execute("SELECT COUNT(*) FROM group_deliveries").fetchone()[0] == 1
            assert c.execute("SELECT COUNT(*) FROM job_matches").fetchone()[0] == 1
            assert not c.execute("PRAGMA foreign_key_check").fetchall()
    third = posting("3", canonical_url=str(jobs[0].canonical_url))
    repo.upsert_job(third)
    assert repo.is_exported(third.id, "client", "csv")


def test_migration_reports_legacy_orphans_without_deleting_them(tmp_path):
    path = tmp_path / "old.db"
    legacy_database(path, [posting()], orphan=True)
    with pytest.raises(ValueError, match="orphaned legacy rows"):
        SQLiteRepository(path)
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT COUNT(*) FROM exports").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_foreign_keys_enabled_on_every_connection(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    with pytest.raises(sqlite3.IntegrityError):
        repo.save_match(
            match_job(posting(), SearchBrief(client_id="c", target_roles=["Support Engineer"]))
        )


def test_later_bridge_preserves_both_existing_delivery_histories(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.db")
    first = posting(description_text="Unknown")
    second = posting("2", description_text="Unknown")
    for job in (first, second):
        repo.upsert_job(job)
        repo.mark_exported(job.id, job.id, "csv")
    bridge = posting("3", canonical_url=str(first.canonical_url), apply_url=str(second.job_url))
    repo.upsert_job(bridge)
    assert repo.is_exported(bridge.id, first.id, "csv")
    assert repo.is_exported(bridge.id, second.id, "csv")
    with repo.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM exports").fetchone()[0] == 2
        assert not c.execute("PRAGMA foreign_key_check").fetchall()


def test_text_normalization_preserves_material_numbers_and_restrictions():
    first = posting()
    whitespace = posting("2", description_text=first.description_text.upper().replace(" ", "  "))
    assert delivery_keys(first) & delivery_keys(whitespace)
    changed = posting(
        "3", description_text=first.description_text + "Must have 7 years experience."
    )
    assert not delivery_keys(first) & delivery_keys(changed)
