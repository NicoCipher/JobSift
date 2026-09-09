from __future__ import annotations

from dataclasses import dataclass

import pytest

from job_scout.domain.models import CollectionResult, Job, SearchBrief, SourceTarget
from job_scout.history import (
    HistoricalBlacklistEvidence,
    HistoricalRecord,
    explicit_blacklist_evidence,
    historical_records,
    operator_status,
    source_identity,
)
from job_scout.orchestration.pipeline import run_pipeline
from job_scout.storage.sqlite import SQLiteRepository


def record(url: str, status: str = "unknown", **changes: object) -> HistoricalRecord:
    source, board, posting = source_identity(url)
    values = {
        "original_url": url,
        "normalized_url": url.split("?")[0].rstrip("/"),
        "source": source,
        "source_board_id": board,
        "source_job_id": posting,
        "title": "Support Engineer",
        "company": "Acme",
        "operator_status": status,
        "source_sheet": "History",
        "source_row": 2,
    }
    values.update(changes)
    return HistoricalRecord(**values)


def job(
    number: str,
    *,
    source: str = "greenhouse",
    board: str = "acme",
    url: str | None = None,
    title: str = "Support Engineer",
    company: str = "Acme",
) -> Job:
    canonical = url or f"https://example.com/jobs/{number}"
    return Job(
        id=f"job-{source}-{board}-{number}",
        source=source,
        source_board_id=board,
        source_job_id=number,
        title=title,
        company=company,
        description_text="Troubleshoot customer systems and investigate Linux networking faults. "
        * 12,
        job_url=canonical,
        canonical_url=canonical,
        country="United States",
        remote_status="remote",
        department="Support",
        content_fingerprint=f"fingerprint-{number}",
    )


@dataclass
class FrozenCollector:
    jobs: list[Job]

    def collect(self, target: SourceTarget) -> CollectionResult:
        return CollectionResult(source="fixture", target=target, jobs=self.jobs, status="success")


def import_records(repo: SQLiteRepository, *records: HistoricalRecord, client: str = "taiwo"):
    return repo.import_historical_records(
        client_id=client, workbook_sha256="a" * 64, records=records
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [("Applied", "applied"), ("Not Applied", "not_applied"), ("", "unknown")],
)
def test_operator_statuses_are_preserved(value, expected):
    assert operator_status(value) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://job-boards.greenhouse.io/acme/jobs/123?utm_source=test",
            ("greenhouse", "acme", "123"),
        ),
        (
            "https://jobs.ashbyhq.com/acme/0c7db8b0-e70a-42f8-9c96-bd680f404401/application",
            ("ashby", "acme", "0c7db8b0-e70a-42f8-9c96-bd680f404401"),
        ),
        (
            "https://jobs.lever.co/acme/0c7db8b0-e70a-42f8-9c96-bd680f404401",
            ("lever", "global:acme", "0c7db8b0-e70a-42f8-9c96-bd680f404401"),
        ),
        (
            "https://jobs.eu.lever.co/acme/0c7db8b0-e70a-42f8-9c96-bd680f404401/apply",
            ("lever", "eu:acme", "0c7db8b0-e70a-42f8-9c96-bd680f404401"),
        ),
        (
            "https://acme.wd5.myworkdayjobs.com/en-US/acme/job/Austin/Support-Engineer_REQ123",
            ("workday", "acme.wd5.myworkdayjobs.com:acme:acme", "REQ123"),
        ),
    ],
)
def test_supported_source_url_identity_contract(url, expected):
    assert source_identity(url) == expected


def test_unresolved_supported_source_and_unsupported_source_are_url_only():
    assert source_identity("https://jobs.lever.co/acme/not-a-uuid") == (None, None, None)
    assert source_identity("https://careers.example.test/jobs/123") == (None, None, None)


@pytest.mark.parametrize("status", ["applied", "not_applied", "unknown"])
def test_all_historical_statuses_suppress_by_exact_identity(tmp_path, status):
    repo = SQLiteRepository(tmp_path / "jobs.sqlite")
    old = job("123", url="https://job-boards.greenhouse.io/acme/jobs/123")
    import_records(repo, record(str(old.canonical_url), status))
    repo.upsert_job(old)
    assert repo.select_deliveries([old], "taiwo", "csv") == []


def test_url_only_suppression_is_conservative_and_client_scoped(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.sqlite")
    old_url = "https://careers.example.test/jobs/123?utm_source=mail"
    import_records(repo, record(old_url, source=None, source_board_id=None, source_job_id=None))
    same_url = job(
        "different",
        source="unsupported",
        board="board",
        url="https://careers.example.test/jobs/123",
    )
    repo.upsert_job(same_url)
    assert repo.select_deliveries([same_url], "taiwo", "csv") == []
    assert repo.select_deliveries([same_url], "another-client", "csv") == [same_url]


def test_title_or_company_alone_never_suppresses_and_different_provider_id_delivers(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.sqlite")
    import_records(repo, record("https://job-boards.greenhouse.io/acme/jobs/123"))
    same_title = job("456", title="Support Engineer", company="Acme")
    repo.upsert_job(same_title)
    assert repo.select_deliveries([same_title], "taiwo", "csv") == [same_title]


def test_import_is_idempotent_and_retains_provenance_without_jobs(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.sqlite")
    item = record("https://job-boards.greenhouse.io/acme/jobs/123", status="applied")
    assert import_records(repo, item) == (1, 0)
    assert import_records(repo, item) == (0, 1)
    with repo.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        row = connection.execute(
            "SELECT original_url, operator_status, source_sheet, source_row FROM historical_job_links"
        ).fetchone()
    assert tuple(row) == (item.original_url, "applied", "History", 2)


def test_pipeline_persists_and_matches_historical_job_but_exports_only_unseen_job(tmp_path):
    repo = SQLiteRepository(tmp_path / "jobs.sqlite")
    historical = job("123", url="https://job-boards.greenhouse.io/acme/jobs/123")
    unseen = job("456", url="https://job-boards.greenhouse.io/acme/jobs/456")
    import_records(repo, record(str(historical.canonical_url)))
    summary = run_pipeline(
        collector=FrozenCollector([historical, unseen]),
        target=SourceTarget(board_id="acme", company="Acme"),
        profile=SearchBrief(client_id="taiwo", target_roles=["Support Engineer"]),
        repository=repo,
        csv_path=tmp_path / "delivery.csv",
    )
    assert summary.received == 2 and summary.matched == 2 and summary.exported == 1
    with repo.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM job_matches").fetchone()[0] == 2


def test_historical_records_reconcile_csv_fixture(tmp_path):
    path = tmp_path / "history.csv"
    path.write_text(
        "sheet,row,title,company,url,status\n"
        "Sheet 1,2,One,Acme,https://job-boards.greenhouse.io/acme/jobs/123,Applied\n"
        "Sheet 1,3,Two,Acme,https://careers.example.test/jobs/456,\n",
        encoding="utf-8",
    )
    rows = historical_records(path)
    assert len(rows) == 2
    assert rows[0].operator_status == "applied"
    assert rows[1].operator_status == "unknown" and rows[1].source is None


def test_not_applied_does_not_create_blacklist_evidence(tmp_path):
    path = tmp_path / "history.csv"
    path.write_text(
        "sheet,row,title,company,url,status\n"
        "Sheet 1,2,One,Acme,https://job-boards.greenhouse.io/acme/jobs/123,Not Applied\n",
        encoding="utf-8",
    )
    assert explicit_blacklist_evidence(path) == []


def test_explicit_blacklist_sheet_is_preserved_separately(tmp_path):
    evidence = [
        HistoricalBlacklistEvidence(
            value="Acme", kind="company", source_sheet="Blacklisted", source_row=1
        )
    ]
    repo = SQLiteRepository(tmp_path / "jobs.sqlite")
    item = record("https://job-boards.greenhouse.io/acme/jobs/123", status="not_applied")
    repo.import_historical_records(
        client_id="taiwo", workbook_sha256="b" * 64, records=[item], blacklist_evidence=evidence
    )
    with repo.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM historical_blacklist_evidence").fetchone()[0]
            == 1
        )


def test_11107_row_reconciliation_import_is_supported(tmp_path):
    statuses = ["applied"] * 9714 + ["not_applied"] * 967 + ["unknown"] * 426
    records = [
        record(
            f"https://careers.example.test/jobs/{index}",
            status=status,
            source=None,
            source_board_id=None,
            source_job_id=None,
            source_row=index + 2,
        )
        for index, status in enumerate(statuses)
    ]
    repo = SQLiteRepository(tmp_path / "jobs.sqlite")
    assert repo.import_historical_records(
        client_id="taiwo", workbook_sha256="c" * 64, records=records
    ) == (11107, 0)
    with repo.connect() as connection:
        counts = dict(
            connection.execute(
                "SELECT operator_status, COUNT(*) FROM historical_job_links GROUP BY operator_status"
            ).fetchall()
        )
    assert counts == {"applied": 9714, "not_applied": 967, "unknown": 426}
