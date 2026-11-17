# Production delivery readiness — 2026-11-08

This is a read-only audit of the deployed JobSift owner workflow and recent GitHub Actions history. No production database queries, writes, sourcing dispatches, or Sheet writes were performed for this audit.

## Findings

### 1. Shared inventory can run, but that is not client delivery

- Refresh Live Job Inventory run **#55** completed successfully on 2026-11-08 at 12:42 UTC.
- The run was a manual `workflow_dispatch`.
- A successful inventory refresh does not prove that a client profile was matched, a review batch was prepared, or rows were written to a client's Google Sheet.

### 2. No recent client provisioning or delivery execution

- In the latest 100 repository workflow runs checked on 2026-11-08, there were no runs for:
  - Provision Operator Client
  - Client Delivery Control
  - Client Review Batch
  - Register Client Sheet
- The older Client Delivery Control run **#7** (2026-10-09 17:57 UTC) failed in `Apply control`.
- Its logs show Neon rejected connections because the project/account had exceeded its quota. The follow-up authoritative snapshot also failed because it could not connect to Neon.
- The older Register Client Sheet run **#1** (2026-10-09 18:15 UTC) failed before registration because `JOBSIFT_CLIENT_SHEET_REGISTRATION_JSON` was missing.

### 3. Production onboarding is currently misconfigured

The deployed frontend's `/api/onboarding` route requires all of the following before it will dispatch client setup:

- `JOBSIFT_GITHUB_TOKEN`
- `JOBSIFT_PROVISIONING_KEY`

The production Vercel environment-variable inventory contains `JOBSIFT_GITHUB_TOKEN`, but does **not** contain `JOBSIFT_PROVISIONING_KEY`. Therefore the current onboarding route returns HTTP 503 with `PROVISIONING_NOT_CONFIGURED` before the provisioning workflow can start.

The new `operator-client-provision.yml` workflow also requires the same `JOBSIFT_PROVISIONING_KEY` as a GitHub Actions secret, alongside the existing Neon and Google service-account secrets. The secret value must be identical on Vercel and GitHub Actions; it must never be placed in source code or browser-visible configuration.

## Required recovery order

1. Create one cryptographically random 32-byte provisioning key, encoded as base64url.
2. Add that exact key as a **sensitive Production** environment variable named `JOBSIFT_PROVISIONING_KEY` in the Vercel `jobsift` project.
3. Add the same key as a GitHub Actions repository secret named `JOBSIFT_PROVISIONING_KEY`.
4. Verify that GitHub Actions has the required `NEON_DATABASE_URL` and `GOOGLE_SERVICE_ACCOUNT_JSON` secrets. Do not print their values.
5. Re-test onboarding with a Sheet inspection, then create one client and confirm the provisioning workflow succeeds.
6. Run the client's delivery preparation in **Review** mode. Confirm the batch contains fresh, unique, eligible jobs.
7. Only after the review is correct, approve the batch and verify the destination Sheet rows and delivery receipt.

Do not use the legacy `register-client-sheet.yml` path as a substitute for the new encrypted provisioning flow.

## Release gate

Do not describe JobSift as delivering jobs until a real client has completed the full path:

`client setup → Sheet verification → shared inventory match → review batch → approved Sheet write → confirmed delivery receipt`.

Keep sourcing cadence and concurrency unchanged while this recovery is completed.