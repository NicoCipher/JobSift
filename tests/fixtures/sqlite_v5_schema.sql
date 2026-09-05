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
