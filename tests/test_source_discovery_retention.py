from datetime import UTC, datetime, timedelta

from job_scout.storage.source_discovery import SourceDiscoveryStore
from job_scout.storage.sqlite import SQLiteRepository


def _observe(store, *, crawl_id: str, observed_at: datetime) -> None:
    store.observe_candidate(
        target_identity="greenhouse:test-board",
        source="greenhouse",
        coordinates={"board": "test-board"},
        company_hint="test-board",
        discovery_source="commoncrawl-cdx",
        crawl_id=crawl_id,
        query_id="greenhouse-global",
        captured_at=observed_at.isoformat(),
        discovered_url="https://job-boards.greenhouse.io/test-board",
        observed_at=observed_at,
    )


def test_evidence_retention_deletes_old_history_but_keeps_latest(tmp_path):
    repository = SQLiteRepository(tmp_path / "retention.sqlite")
    store = SourceDiscoveryStore(repository)
    now = datetime(2026, 11, 5, tzinfo=UTC)

    _observe(store, crawl_id="old-crawl", observed_at=now - timedelta(days=60))
    _observe(store, crawl_id="recent-crawl", observed_at=now - timedelta(days=1))
    store.record_health(
        target_identity="greenhouse:test-board",
        checked_at=now - timedelta(days=60),
        result={"classification": "active", "current_postings": 2, "inventory_exact": True,
                "http_status": 200, "request_count": 1, "error": None},
    )
    store.record_health(
        target_identity="greenhouse:test-board",
        checked_at=now - timedelta(days=1),
        result={"classification": "active", "current_postings": 3, "inventory_exact": True,
                "http_status": 200, "request_count": 1, "error": None},
    )

    deleted = store.prune_evidence(now=now, retention_days=30)

    assert deleted == {
        "deleted_discovery_evidence": 1,
        "deleted_health_evidence": 1,
    }
    with repository.connect() as connection:
        discovery = connection.execute(
            "SELECT crawl_id FROM source_discovery_evidence"
        ).fetchall()
        health = connection.execute(
            "SELECT current_postings FROM source_discovery_health_evidence"
        ).fetchall()
    assert [row["crawl_id"] for row in discovery] == ["recent-crawl"]
    assert [row["current_postings"] for row in health] == [3]


def test_evidence_retention_preserves_only_record_when_no_newer_record_exists(tmp_path):
    repository = SQLiteRepository(tmp_path / "latest.sqlite")
    store = SourceDiscoveryStore(repository)
    now = datetime(2026, 11, 5, tzinfo=UTC)
    _observe(store, crawl_id="only-crawl", observed_at=now - timedelta(days=90))

    deleted = store.prune_evidence(now=now, retention_days=30)

    assert deleted["deleted_discovery_evidence"] == 0
    with repository.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM source_discovery_evidence"
        ).fetchone()[0]
    assert count == 1
