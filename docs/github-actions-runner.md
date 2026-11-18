# GitHub Actions + Neon live runner

JobSift's production runner uses GitHub Actions for compute and Neon/Postgres for
durable state. No always-on server or persistent Render disk is required.

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
  fresh local SQLite database with the Neon/Postgres backend disabled. It returns a `validated`
  result, cannot release or discard, does not alter production delivery history,
  and performs no Google Sheets write. Use it for same-day policy/source checks
  without bypassing the production one-batch-per-day guard.
- GitHub Actions concurrency allows only one production run at a time.
- The stable client ID and Neon database preserve historical/delivery
  suppression across ephemeral GitHub runners.

## Neon persistence

The production workflow sets `JOBSIFT_PERSISTENCE_BACKEND=postgres` and reads
`NEON_DATABASE_URL` from GitHub Actions secrets. Each run uses an ephemeral
GitHub-hosted runner while JobSift's durable inventory, match, and delivery state
remain in the Neon database. Manual CSV export and Google Sheets delivery use the
same production database and existing workflow concurrency locks.

Production runs fail before sourcing when `NEON_DATABASE_URL` is unavailable.
Validation mode is the explicit exception: it switches to an isolated local
SQLite database and does not touch production state.

## Required repository secrets

In GitHub repository Settings > Secrets and variables > Actions > Secrets, add:

- `NEON_DATABASE_URL`
- `GOOGLE_SERVICE_ACCOUNT_JSON` (Google Sheets delivery only)

`GOOGLE_SERVICE_ACCOUNT_JSON` is the complete service-account key JSON. Never
commit it to the repository. Manual CSV export does not require or receive this
credential.

## Required repository variables

In Settings > Secrets and variables > Actions > Variables, configure these only
when using the legacy Google Sheets destination path:

- `JOBSIFT_SHEET_ID`
- `JOBSIFT_SHEET_TAB`

Manual CSV export does not require either variable. For the current production
Sheet path, the tab is `Sheet1`.

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

## Operator-owned manual CSV export

The same live runner has an explicit `JOBSIFT_MANUAL_EXPORT=true` mode, exposed
as the **manual_export** input in Actions > Live JobSift. It uses the verified
production sourcing plan and durable database, but sets the batch destination to
the runner-owned CSV path. It does not resolve a registered client destination,
read the legacy Sheet variables, create Google credentials, or call Google Sheets.

Manual export finalizes the frozen batch through the existing journalled CSV
publisher, then uploads the CSV and run-result JSON as a GitHub Actions artifact.
Artifacts are named with the workflow run and attempt IDs and retained for three
days. The artifact is an operator handoff, not a client Sheet publication or
proof that a client received the jobs.

If a successful CSV export is delivered to the database but artifact upload fails,
a retry on the same local day rebuilds the missing ephemeral CSV from the
immutable delivered batch rows before uploading. It does not source a second
batch or append duplicate rows. The runner's existing production concurrency
locks remain in force. Manual export cannot be combined with Sheet release,
discard, validation, or profile-managed delivery.

To use it, open Actions > Live JobSift > Run workflow, set **manual_export=true**,
leave release/discard/validate false, and choose quota 5 for the first controlled
run. After success, download the `jobsift-manual-export-*` artifact and inspect
both the CSV and run-result JSON. A partial/failed source run remains blocked by
`JOBSIFT_ALLOW_PARTIAL=false`; do not widen the source set or concurrency as part
of this first export.

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
