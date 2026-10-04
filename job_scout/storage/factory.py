"""Select JobSift persistence explicitly; local/Turso remains the default."""

from __future__ import annotations

import os
from pathlib import Path

from job_scout.storage.sqlite import SQLiteRepository

_POSTGRES_BACKENDS = {"postgres", "postgresql", "neon"}
_SQLITE_BACKENDS = {"sqlite", "turso"}


def selected_backend() -> str:
    """Return the explicit persistence backend; never infer cutover from a secret alone."""
    value = os.getenv("JOBSIFT_PERSISTENCE_BACKEND", "sqlite").strip().casefold()
    if value in _SQLITE_BACKENDS:
        return "sqlite"
    if value in _POSTGRES_BACKENDS:
        return "postgres"
    raise ValueError(
        "JOBSIFT_PERSISTENCE_BACKEND must be sqlite/turso or postgres/neon"
    )


def postgres_database_url() -> str | None:
    """Return the Neon/Postgres URL only when Postgres is explicitly selected."""
    if selected_backend() != "postgres":
        return None
    database_url = os.getenv("NEON_DATABASE_URL", "").strip()
    if not database_url:
        raise ValueError(
            "NEON_DATABASE_URL is required when JOBSIFT_PERSISTENCE_BACKEND=postgres"
        )
    return database_url


def create_repository(path: str | Path):
    """Create the configured repository without changing local/test defaults."""
    database_url = postgres_database_url()
    if database_url is None:
        return SQLiteRepository(path)

    from job_scout.storage.postgres import PostgresRepository

    return PostgresRepository(database_url)
