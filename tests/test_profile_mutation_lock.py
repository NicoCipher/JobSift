from datetime import UTC, datetime, timedelta

import pytest

from job_scout.profile_mutation_lock import (
    LOCK_NAME,
    ProfileMutationLock,
    ProfileMutationLockError,
    profile_mutation_guard,
)
from job_scout.storage.sqlite import SQLiteRepository


def test_profile_mutation_lock_blocks_second_owner_until_release(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    first = ProfileMutationLock(SQLiteRepository(path))
    second = ProfileMutationLock(SQLiteRepository(path))

    first_token = first.acquire(owner_label="first", wait_seconds=0)
    first.assert_owned(first_token)

    with pytest.raises(ProfileMutationLockError, match="busy"):
        second.acquire(owner_label="second", wait_seconds=0)

    assert first.release(first_token) is True
    second_token = second.acquire(owner_label="second", wait_seconds=0)
    second.assert_owned(second_token)
    assert second.release(second_token) is True


def test_profile_mutation_lock_allows_takeover_after_expiry(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    repository = SQLiteRepository(path)
    first = ProfileMutationLock(repository)
    second = ProfileMutationLock(repository)

    first_token = first.acquire(owner_label="first", wait_seconds=0)
    with repository.connect() as connection:
        connection.execute(
            "UPDATE profile_mutation_locks SET expires_at=? WHERE lock_name=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), LOCK_NAME),
        )

    second_token = second.acquire(owner_label="second", wait_seconds=0)
    assert second_token != first_token
    second.assert_owned(second_token)


def test_profile_mutation_guard_releases_after_failure(tmp_path):
    repository = SQLiteRepository(tmp_path / "jobs.sqlite3")

    with pytest.raises(RuntimeError, match="boom"):
        with profile_mutation_guard(
            repository,
            owner_label="failing-operation",
            wait_seconds=0,
        ):
            raise RuntimeError("boom")

    lock = ProfileMutationLock(repository)
    token = lock.acquire(owner_label="next-operation", wait_seconds=0)
    lock.assert_owned(token)
    assert lock.release(token) is True
