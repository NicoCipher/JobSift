# Live runner

The live runner connects a verified sourcing plan to one persistent SQLite database and one Google Sheets destination.

## Safety defaults

The committed pilot configuration:

- uses the verified `taiwo-software-remote-us-pilot-v1` sourcing plan;
- stores SQLite and run artifacts under the Render persistent disk;
- prepares at most 5 jobs per batch;
- blocks batch preparation when sourcing is partial or failed;
- does not release to Google Sheets until `JOBSIFT_AUTO_RELEASE=true`;
- reuses an unresolved prepared/failed batch instead of creating a competing one;
- allows only one batch for the configured client/destination per local calendar day.

The live runner never stores Google credentials or the spreadsheet ID in source control.

## Render setup

Deploy the root `render.yaml` as a Blueprint. The worker requires a persistent disk because SQLite is the authoritative delivery-history store.

Set these environment values in Render:

- `JOBSIFT_SHEET_ID`: the authorized production spreadsheet ID.
- `JOBSIFT_SHEET_TAB`: the exact destination tab name.

Upload the Google service-account JSON as a Render secret file named
`google-service-account.json`. The Blueprint points Application Default
Credentials at `/etc/secrets/google-service-account.json`.

The spreadsheet must already be shared with that service account as an editor.

## First pilot

Keep `JOBSIFT_AUTO_RELEASE=false` for the first run. The worker sources the
verified targets and freezes up to 5 fresh jobs in SQLite. Its JSON log includes
the batch ID and compact selected-job links.

After review, set `JOBSIFT_AUTO_RELEASE=true` and redeploy/restart the worker.
It sees the unresolved prepared batch first and releases that exact frozen batch;
it does not source a replacement batch.

After confirming the first release and rerun suppression, increase
`JOBSIFT_BATCH_QUOTA` toward the production target.
