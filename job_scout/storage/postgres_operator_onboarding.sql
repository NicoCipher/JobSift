-- Additive JobSift operator onboarding schema.
-- Safe to apply to an existing postgres-v1 database before deploying the
-- frontend onboarding routes. Existing sourcing and delivery tables are unchanged.

CREATE TABLE IF NOT EXISTS operator_clients (
  client_id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  destination_id TEXT NOT NULL,
  destination_name TEXT NOT NULL,
  sourcing_plan_id TEXT NOT NULL UNIQUE,
  search_brief_json TEXT NOT NULL,
  brief_sha256 TEXT NOT NULL,
  brief_revision INTEGER NOT NULL CHECK(brief_revision > 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(client_id, destination_id)
);

CREATE TABLE IF NOT EXISTS operator_provisioning_requests (
  request_id TEXT PRIMARY KEY,
  operation TEXT NOT NULL CHECK(operation IN (
    'inspect_sheet','create_client','update_criteria','update_sheet'
  )),
  state TEXT NOT NULL CHECK(state IN ('queued','running','completed','failed')),
  request_sha256 TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  result_json TEXT,
  error_code TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_operator_provisioning_requests_state
  ON operator_provisioning_requests(state, created_at, request_id);
