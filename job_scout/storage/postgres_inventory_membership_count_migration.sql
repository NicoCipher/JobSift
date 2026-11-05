-- Preserve idempotent inventory-run replay metrics before expiring membership links.
-- Safe to run once on an existing PostgreSQL deployment; all changes are additive.
BEGIN;

ALTER TABLE inventory_runs
  ADD COLUMN IF NOT EXISTS membership_count BIGINT NOT NULL DEFAULT 0
  CHECK (membership_count >= 0);

UPDATE inventory_runs AS runs
SET membership_count = (
  SELECT COUNT(*)
  FROM inventory_run_jobs AS memberships
  WHERE memberships.run_id = runs.run_id
);

COMMIT;
