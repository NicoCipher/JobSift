"""Select the persistence backend without changing local/test defaults."""

from __future__ import annotations

import os
from pathlib import Path

from job_scout.storage.sqlite import SQLiteRepository


def create_repository(path: str | Path):
    """Use Neon Postgres when configured; otherwise preserve local SQLite."""
    database_url = os.getenv("NEON_DATABASE_URL", "").strip()
    if not database_url:
        return SQLiteRepository(path)

    from job_scout.storage.postgres import PostgresRepository

    return PostgresRepository(database_url)
