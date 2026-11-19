from datetime import UTC, datetime, timedelta

import pytest

from job_scout.storage.source_discovery import SourceDiscoveryStore
from job_scout.storage.sqlite import SQLiteRepository


NOW = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)


def _observe(store: SourceDiscoveryStore, identity: str, observed_at: datetime) -> None:
    store.observe_candidate(
        target_identity=identity,
        source="greenhouse",
        coordinates={"board": identity.split(":", 1)[1]},
        company_hint=identity,
        discovery_source="commoncrawl-cdx",
        crawl_id="CC-MAIN-test",
        query_id="greenhouse-global",
        captured_at=observed_at.strftime("%Y%m%d%H%M%S"),
        discovered_url=f"https://job-boards.greenhouse.io/{identity.split(':', 1)[1]}",
        observed_at=observed_at,
    )


def _record_health(store: SourceDiscoveryStore, identity: str, checked_at: datetime) -> None:
    store.record_health(
        target_identity=identity,
        checked_at=checked_at,
        result={
            "classification": "invalid",
            "current_postings": None,
            "inventory_exact": None,
            "http_status": 404,
            "request_count": 1,
            "error": "HTTP 404",
        },
    )


def test_prune_inactive_targets_cascades_only_stale_never_admitted_evidence(tmp_path):
    repository = SQLiteRepository(tmp_path / "targets.sqlite")
    store = SourceDiscoveryStore(repository)

    stale_checked = "greenhouse:stale-checked"
    stale_unchecked = "greenhouse:stale-unchecked"
    recently_seen = "greenhouse:recent"
    admitted = "greenhouse:admitted"

    old = NOW - timedelta(days=45)
    _observe(store, stale_checked, old)
    _record_health(store, stale_checked, old)
    _observe(store, stale_unchecked, old)
    _observe(store, recently_seen, NOW - timedelta(days=2))
    _observe(store, admitted, old)
    store.record_health(
        target_identity=admitted,
        checked_at=old,
        result={
            "classification": "active",
            "current_postings": 12,
            "inventory_exact": True,
            "http_status": 200,
            "request_count": 1,
            "error": None,
        },
    )

    result = store.prune_inactive_targets(now=NOW, retention_days=30, batch_size=1)

    assert result["deleted_inactive_never_admitted_targets"] == 2
    with repository.connect() as connection:
        targets = {
            row["target_identity"]
            for row in connection.execute(
                "SELECT target_identity FROM source_discovery_targets"
            ).fetchall()
        }
        discovery = {
            row["target_identity"]
            for row in connection.execute(
                "SELECT target_identity FROM source_discovery_evidence"
            ).fetchall()
        }
        health = {
            row["target_identity"]
            for row in connection.execute(
                "SELECT target_identity FROM source_discovery_health_evidence"
            ).fetchall()
        }

    assert targets == {recently_seen, admitted}
    assert discovery == {recently_seen, admitted}
    assert health == {admitted}


def test_prune_inactive_targets_preserves_due_again_candidates_and_recent_health(tmp_path):
    repository = SQLiteRepository(tmp_path / "protected.sqlite")
    store = SourceDiscoveryStore(repository)
    old = NOW - timedelta(days=45)

    due_again = "greenhouse:due-again"
    recently_checked = "greenhouse:recent-health"
    _observe(store, due_again, old)
    _record_health(store, due_again, old)
    _observe(store, recently_checked, old)
    _record_health(store, recently_checked, NOW - timedelta(days=1))

    # Move the first target's next check into the future without changing its
    # historical health timestamp, simulating a deferred retry.
    with repository.connect() as connection:
        connection.execute(
            "UPDATE source_discovery_targets SET next_health_check_at=? "
            "WHERE target_identity=?",
            ((NOW + timedelta(days=1)).isoformat(), due_again),
        )

    result = store.prune_inactive_targets(now=NOW, retention_days=30)

    assert result["deleted_inactive_never_admitted_targets"] == 0
    with repository.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM source_discovery_targets"
        ).fetchone()[0]
    assert count == 2


@pytest.mark.parametrize(
    ("retention_days", "batch_size"),
    [(0, 10), (30, 0)],
)
def test_prune_inactive_targets_rejects_invalid_limits(
    tmp_path, retention_days, batch_size
):
    store = SourceDiscoveryStore(SQLiteRepository(tmp_path / "invalid.sqlite"))
    with pytest.raises(ValueError):
        store.prune_inactive_targets(
            now=NOW,
            retention_days=retention_days,
            batch_size=batch_size,
        )
