"""Database-backed serialization for delivery-profile mutations.

GitHub workflow concurrency serializes normal operator workflows, but direct CLI
commands can bypass GitHub Actions. This lease closes that gap by storing a
short-lived owner token in the same persistent database used for delivery state.
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from job_scout.storage.sqlite import SQLiteRepository

LOCK_NAME = "client-delivery-profile"
LOCK_SCHEMA = """
CREATE TABLE IF NOT EXISTS profile_mutation_locks (
  lock_name TEXT PRIMARY KEY,
  owner_token TEXT NOT NULL,
  owner_label TEXT NOT NULL,
  acquired_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
"""


class ProfileMutationLockError(RuntimeError):
    """Raised when a delivery-profile mutation lease cannot be acquired or verified."""


class ProfileMutationLock:
    def __init__(self, repository) -> None:
        self.repository = repository
        with repository.connect() as connection:
            self._pull_remote(connection)
            connection.executescript(LOCK_SCHEMA)

    def _pull_remote(self, connection) -> None:
        if getattr(self.repository, "remote_url", ""):
            connection.pull()

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def acquire(
        self,
        *,
        owner_label: str,
        ttl_seconds: int = 600,
        wait_seconds: float = 30,
        token: str | None = None,
    ) -> str:
        if ttl_seconds < 1:
            raise ValueError("profile mutation lock TTL must be at least one second")
        if wait_seconds < 0:
            raise ValueError("profile mutation lock wait must not be negative")
        owner_label = owner_label.strip()
        if not owner_label:
            raise ValueError("profile mutation lock owner must not be blank")

        owner_token = token or uuid4().hex
        deadline = time.monotonic() + wait_seconds
        while True:
            now = datetime.now(UTC)
            expires_at = now + timedelta(seconds=ttl_seconds)
            try:
                with self.repository.connect() as connection:
                    self._pull_remote(connection)
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "INSERT INTO profile_mutation_locks "
                        "(lock_name,owner_token,owner_label,acquired_at,expires_at) "
                        "VALUES (?,?,?,?,?) "
                        "ON CONFLICT(lock_name) DO UPDATE SET "
                        "owner_token=excluded.owner_token,"
                        "owner_label=excluded.owner_label,"
                        "acquired_at=excluded.acquired_at,"
                        "expires_at=excluded.expires_at "
                        "WHERE profile_mutation_locks.expires_at<=? "
                        "OR profile_mutation_locks.owner_token=excluded.owner_token",
                        (
                            LOCK_NAME,
                            owner_token,
                            owner_label,
                            now.isoformat(),
                            expires_at.isoformat(),
                            now.isoformat(),
                        ),
                    )
                    row = connection.execute(
                        "SELECT owner_token,expires_at FROM profile_mutation_locks "
                        "WHERE lock_name=?",
                        (LOCK_NAME,),
                    ).fetchone()
                    acquired = (
                        row is not None
                        and row["owner_token"] == owner_token
                        and self._aware(datetime.fromisoformat(row["expires_at"])) > now
                    )
                if acquired:
                    return owner_token
            except sqlite3.DatabaseError:
                if time.monotonic() >= deadline:
                    raise

            if time.monotonic() >= deadline:
                raise ProfileMutationLockError(
                    "delivery-profile mutation lock is busy; retry after the active operation finishes"
                )
            time.sleep(0.25)

    def assert_owned(self, token: str) -> None:
        token = token.strip()
        if not token:
            raise ProfileMutationLockError("profile mutation lock token is missing")
        now = datetime.now(UTC)
        with self.repository.connect() as connection:
            self._pull_remote(connection)
            row = connection.execute(
                "SELECT owner_token,expires_at FROM profile_mutation_locks WHERE lock_name=?",
                (LOCK_NAME,),
            ).fetchone()
        if row is None or row["owner_token"] != token:
            raise ProfileMutationLockError("profile mutation lock ownership was lost")
        if self._aware(datetime.fromisoformat(row["expires_at"])) <= now:
            raise ProfileMutationLockError("profile mutation lock expired")

    def release(self, token: str) -> bool:
        token = token.strip()
        if not token:
            return False
        with self.repository.connect() as connection:
            self._pull_remote(connection)
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM profile_mutation_locks WHERE lock_name=? AND owner_token=?",
                (LOCK_NAME, token),
            )
            return cursor.rowcount == 1


@contextmanager
def profile_mutation_guard(
    repository,
    *,
    owner_label: str,
    ttl_seconds: int = 600,
    wait_seconds: float = 30,
):
    lock = ProfileMutationLock(repository)
    token = lock.acquire(
        owner_label=owner_label,
        ttl_seconds=ttl_seconds,
        wait_seconds=wait_seconds,
    )
    try:
        yield token
    finally:
        lock.release(token)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m job_scout.profile_mutation_lock")
    commands = parser.add_subparsers(dest="command", required=True)

    acquire = commands.add_parser("acquire")
    acquire.add_argument("--database", type=Path, required=True)
    acquire.add_argument("--owner", required=True)
    acquire.add_argument("--ttl-seconds", type=int, default=600)
    acquire.add_argument("--wait-seconds", type=float, default=30)

    verify = commands.add_parser("assert")
    verify.add_argument("--database", type=Path, required=True)
    verify.add_argument("--token", required=True)

    release = commands.add_parser("release")
    release.add_argument("--database", type=Path, required=True)
    release.add_argument("--token", required=True)

    args = parser.parse_args()
    try:
        args.database.parent.mkdir(parents=True, exist_ok=True)
        repository = SQLiteRepository(args.database)
        lock = ProfileMutationLock(repository)
        if args.command == "acquire":
            print(
                lock.acquire(
                    owner_label=args.owner,
                    ttl_seconds=args.ttl_seconds,
                    wait_seconds=args.wait_seconds,
                )
            )
        elif args.command == "assert":
            lock.assert_owned(args.token)
            print("locked")
        else:
            lock.release(args.token)
            print("released")
    except (OSError, ProfileMutationLockError, ValueError, sqlite3.Error) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
