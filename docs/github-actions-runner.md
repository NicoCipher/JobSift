# GitHub Actions + Turso live runner

JobSift's production runner uses GitHub Actions for compute and Turso for durable
SQLite-compatible state. No always-on server or persistent Render disk is required.

## Safety model

- The scheduled workflow runs daily at 07:00 UTC (08:00 WAT).
- Scheduled runs always use `JOBSIFT_AUTO_RELEASE=false`.
- A scheduled run may source jobs and freeze up to 5 fresh links, but it cannot
  publish them to Google Sheets.
- An unresolved prepared or failed batch blocks replacement sourcing.
- To publish the exact frozen batch, manually run the workflow with
  `release=true`.
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

## First pilot

1. Configure the secrets and variables above.
2. Open Actions > Live JobSift > Run workflow.
3. Keep `release=false` and quota `5`.
4. Inspect the run's final JSON payload and its selected job links.
5. If the batch is correct, run the workflow again with `release=true`.
6. Verify the five rows in Google Sheets.
7. Run it again with `release=true`; it must not duplicate the delivered batch.
8. After the pilot, increase the quota when desired.

The workflow also runs automatically once per day, but it never releases a batch
without the explicit manual release switch.
