# GitHub Actions + Turso live runner

JobSift's production runner uses GitHub Actions for compute and Turso for durable
SQLite-compatible state. No always-on server or persistent Render disk is required.

## Safety model

- The scheduled workflow runs daily at 07:00 UTC (08:00 WAT).
- Scheduled runs always use `JOBSIFT_AUTO_RELEASE=false`.
- A scheduled run may source jobs and freeze up to 5 fresh links, but it cannot
  publish them to Google Sheets.
- Live batches apply the client delivery policy after matching. The current
  production policy allows at most one job per employer per batch. Extra matching
  roles remain persisted for other clients or later policy-eligible batches.
- If fewer distinct eligible employers exist than the requested quota, the run
  returns an explicit shortfall instead of padding with repeated companies.
- An unresolved prepared or failed batch blocks replacement sourcing.
- To publish the exact frozen batch, manually run the workflow with
  `release=true`.
- Release mode is strictly release-only: if there is no unresolved frozen batch,
  it exits with `nothing_to_release` and does not source or publish new jobs.
- Manual discard mode can remove only an unreleased `prepared` batch. It refuses
  delivered batches and any batch with an export journal, so uncertain Sheet I/O
  can never be erased.
- Manual validation mode runs the real sourcing plan and SearchBrief against a
  fresh local SQLite database with Turso disabled. It returns a `validated`
  result, cannot release or discard, does not alter production delivery history,
  and performs no Google Sheets write. Use it for same-day policy/source checks
  without bypassing the production one-batch-per-day guard.
- GitHub Actions concurrency allows only one production run at a time.
- The stable Taiwo client ID and Turso database preserve historical/delivery
  suppression across ephemeral GitHub runners.

## Turso persistence

When both `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` are present,
`SQLiteRepository` opens a local Turso Sync replica at the configured
`JOBSIFT_DATABASE` path.

Each GitHub Actions run starts with a fresh local database path. Turso Sync
bootstraps that local replica from the remote database when it is first opened.
The workflow concurrency lock guarantees there is only one production writer.

For each repository transaction JobSift then:

1. opens the synced local replica;
2. executes the existing SQLite transaction locally;
3. commits locally;
4. pushes the committed transaction to Turso.

JobSift deliberately does not long-poll Turso before every small repository
operation. A fresh Actions run already bootstraps remote state, and the
single-writer lock prevents another production run from changing it underneath
the current run.

This is important for Sheets recovery. The batch delivery journal is pushed to
Turso before external Sheet I/O. If a run dies after a Sheet append, the next run
can pull that journal, inspect the Sheet after-image, and finish without
duplicating the rows.

Without the Turso environment variables, JobSift keeps using the standard local
SQLite backend for development and tests.

## Required repository secrets

In GitHub repository Settings > Secrets and variables > Actions > Secrets, add:

- `TURSO_DATABASE_URL`
- `TURSO_AUTH_TOKEN`
- `GOOGLE_SERVICE_ACCOUNT_JSON`

`GOOGLE_SERVICE_ACCOUNT_JSON` is the complete service-account key JSON. Never
commit it to the repository.

## Required repository variables

In Settings > Secrets and variables > Actions > Variables, add:

- `JOBSIFT_SHEET_ID`
- `JOBSIFT_SHEET_TAB`

For the current production sheet, the tab is `Sheet1`.

## Production source set

The live workflow now uses
`config/sourcing_plans/taiwo_software_remote_us_v2.json`, a seven-employer
source set documented in [Production software sources V2](production-sources-v2.md).

The seven-employer set is intentionally bounded. It is large enough to test a
five-job, five-employer batch, but it is not evidence that 25, 50, or 100 distinct
employers can be supplied reliably. Production expansion should follow measured
distinct-employer yield.

`JOBSIFT_ALLOW_PARTIAL=false` remains in force. A source failure or partial
target blocks preparation rather than silently pretending the source set was
complete. This is conservative and may become an availability bottleneck as the
target count grows; change it only with an explicit completeness policy.

## First pilot

1. Configure the secrets and variables above.
2. Open Actions > Live JobSift > Run workflow.
3. For a production prepare, keep `release=false`, `discard=false`,
   `validate=false`, and quota `5`. For a same-day safety check after changing
   policy or sources, use `validate=true` with both release and discard false.
4. Inspect the run's final JSON payload and its selected job links.
5. If any frozen link is wrong, run the workflow with `discard=true`,
   `release=false`, then prepare a replacement batch.
6. If the batch is correct, run the workflow again with `release=true`.
7. Verify the five rows in Google Sheets.
8. Run it again with `release=true`; it must not duplicate the delivered batch.
9. After the pilot, increase the quota when desired.

The workflow also runs automatically once per day, but it never releases a batch
without the explicit manual release switch.
