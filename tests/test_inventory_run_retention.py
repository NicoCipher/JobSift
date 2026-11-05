from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from job_scout.storage.inventory_runs import InventoryRunStore
from job_scout.storage.sqlite import SQLiteRepository


def _repository_with_job(tmp_path):
    repository = SQLiteRepository(tmp_path / "inventory.sqlite3")
    now = datetime.now(UTC).isoformat()
    with repository.connect() as connection:
        connection.execute(
            "INSERT INTO jobs (id,source,source_job_id,source_board_id,canonical_url,"
            "content_fingerprint,first_seen_at,last_seen_at,last_verified_at,lifecycle,"
            "payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "job-1",
                "greenhouse",
                "job-1",
                "board-1",
                "https://example.test/job-1",
                "fingerprint-1",
                now,
                now,
                now,
                "active",
                "{}",
            ),
        )
    return repository


def test_prune_memberships_removes_only_expired_completed_run_links(tmp_path) -> None:
    repository = _repository_with_job(tmp_path)
    store = InventoryRunStore(repository)
    now = datetime(2026, 11, 5, 12, tzinfo=UTC)
    old_completed = now - timedelta(days=4)
    recent_completed = now - timedelta(hours=1)

    for run_id, completed_at in (
        ("old-run", old_completed),
        ("recent-run", recent_completed),
    ):
        store.create_if_absent(
            run_id=run_id,
            plan_id="plan",
            started_at=completed_at - timedelta(minutes=5),
        )
        store.add_memberships(
            run_id=run_id,
            memberships=[("greenhouse:board-1", "job-1")],
        )
        store.finish(run_id=run_id, status="success", completed_at=completed_at)

    store.create_if_absent(
        run_id="running-run",
        plan_id="plan",
        started_at=now - timedelta(days=5),
    )
    store.add_memberships(
        run_id="running-run",
        memberships=[("greenhouse:board-1", "job-1")],
    )

    with repository.connect() as connection:
        connection.execute(
            "INSERT INTO inventory_target_observations "
            "(run_id,target_identity,source,started_at,completed_at,status,runtime_ms,"
            "raw_postings_received,normalized_jobs,postings_with_trustworthy_timestamps,"
            "postings_at_most_24h_old) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "old-run",
                "greenhouse:board-1",
                "greenhouse",
                old_completed.isoformat(),
                old_completed.isoformat(),
                "success",
                100,
                1,
                1,
                1,
                1,
            ),
        )

    result = store.prune_memberships(
        retention_hours=72,
        evaluated_at=now,
    )

    assert result == {
        "membership_rows_deleted": 1,
        "membership_retention_hours": 72,
    }
    with repository.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM inventory_run_jobs WHERE run_id='old-run'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM inventory_run_jobs WHERE run_id='recent-run'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM inventory_run_jobs WHERE run_id='running-run'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM inventory_runs"
        ).fetchone()[0] == 3
        assert connection.execute(
            "SELECT COUNT(*) FROM inventory_target_observations WHERE run_id='old-run'"
        ).fetchone()[0] == 1


def test_prune_memberships_rejects_non_positive_retention(tmp_path) -> None:
    store = InventoryRunStore(_repository_with_job(tmp_path))

    with pytest.raises(ValueError, match="retention_hours must be at least 1"):
        store.prune_memberships(retention_hours=0)
