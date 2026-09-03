from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from job_scout.domain.models import Job, JobLifecycle, JobMatch

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  source_job_id TEXT NOT NULL,
  source_board_id TEXT NOT NULL,
  canonical_url TEXT NOT NULL,
  content_fingerprint TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  last_verified_at TEXT NOT NULL,
  lifecycle TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  UNIQUE(source, source_board_id, source_job_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_canonical_url ON jobs(canonical_url);
CREATE TABLE IF NOT EXISTS job_matches (
  job_id TEXT NOT NULL REFERENCES jobs(id),
  client_id TEXT NOT NULL,
  decision TEXT NOT NULL,
  score INTEGER,
  matched_reasons_json TEXT NOT NULL,
  rejection_reasons_json TEXT NOT NULL,
  evaluated_at TEXT NOT NULL,
  matcher_version TEXT NOT NULL,
  PRIMARY KEY(job_id, client_id)
);
CREATE TABLE IF NOT EXISTS collection_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL, target TEXT NOT NULL, started_at TEXT NOT NULL,
  completed_at TEXT, status TEXT NOT NULL, metrics_json TEXT NOT NULL, errors_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exports (
  job_id TEXT NOT NULL REFERENCES jobs(id),
  client_id TEXT NOT NULL,
  destination TEXT NOT NULL,
  exported_at TEXT NOT NULL,
  PRIMARY KEY(job_id, client_id, destination)
);
"""


class SQLiteRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def upsert_job(self, job: Job) -> JobLifecycle:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, content_fingerprint FROM jobs WHERE source=? AND source_board_id=? AND source_job_id=?",
                (job.source, job.source_board_id, job.source_job_id),
            ).fetchone()
            if row is None:
                duplicate = connection.execute(
                    "SELECT id FROM jobs WHERE canonical_url=?", (str(job.canonical_url),)
                ).fetchone()
                if duplicate:
                    return JobLifecycle.SEEN
                connection.execute(
                    "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        job.id,
                        job.source,
                        job.source_job_id,
                        job.source_board_id,
                        str(job.canonical_url),
                        job.content_fingerprint,
                        job.discovered_at.isoformat(),
                        job.last_seen_at.isoformat(),
                        now,
                        JobLifecycle.NEW.value,
                        job.model_dump_json(),
                    ),
                )
                return JobLifecycle.NEW
            lifecycle = (
                JobLifecycle.CHANGED
                if row["content_fingerprint"] != job.content_fingerprint
                else JobLifecycle.SEEN
            )
            job.id = row["id"]
            connection.execute(
                "UPDATE jobs SET canonical_url=?, content_fingerprint=?, last_seen_at=?, last_verified_at=?, lifecycle=?, payload_json=? WHERE id=?",
                (
                    str(job.canonical_url),
                    job.content_fingerprint,
                    job.last_seen_at.isoformat(),
                    now,
                    lifecycle.value,
                    job.model_dump_json(),
                    job.id,
                ),
            )
            return lifecycle

    def save_match(self, match: JobMatch) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO job_matches VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    match.job_id,
                    match.client_id,
                    match.decision.value,
                    match.score,
                    json.dumps(match.matched_reasons),
                    json.dumps(match.rejection_reasons),
                    match.evaluated_at.isoformat(),
                    match.matcher_version,
                ),
            )

    def is_exported(self, job_id: str, client_id: str, destination: str) -> bool:
        with self.connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM exports WHERE job_id=? AND client_id=? AND destination=?",
                    (job_id, client_id, destination),
                ).fetchone()
                is not None
            )

    def mark_exported(self, job_id: str, client_id: str, destination: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO exports VALUES (?, ?, ?, ?)",
                (job_id, client_id, destination, datetime.now(UTC).isoformat()),
            )
