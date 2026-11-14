-- Add the audit table required by stale scheduler backlog recovery.
-- Safe to apply repeatedly; does not alter scheduler state or inventory data.
CREATE TABLE IF NOT EXISTS inventory_refresh_schedule_recovery_audit (
  recovery_id TEXT PRIMARY KEY,
  schedule_key TEXT NOT NULL,
  previous_completed_cohort BIGINT NOT NULL,
  resumed_before_cohort BIGINT NOT NULL,
  skipped_cohorts BIGINT NOT NULL CHECK(skipped_cohorts > 0),
  recovered_at TEXT NOT NULL,
  reason TEXT NOT NULL
);
