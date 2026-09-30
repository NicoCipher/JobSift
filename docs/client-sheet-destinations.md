# Client-owned Google Sheets delivery

JobSift treats a client's Google Sheet as a delivery destination, not as its database.

## Client onboarding

The current production Sheets identity is:

`jobsift-sheets-publisher@jobsift-510120.iam.gserviceaccount.com`

The client shares the worksheet with that identity as **Editor** and supplies the
Google Sheets link to the operator. Do not ask a client for Google account
credentials, OAuth tokens, or service-account keys.

A destination is registered against three explicit identities:

- `client_id`: the owner of the delivery data;
- `destination_id`: JobSift's stable logical identity for that worksheet;
- `campaign_id`: the active client delivery campaign using the destination.

The physical spreadsheet ID, stable Google worksheet ID (`gid`), worksheet name,
header row, header fingerprint, and column mapping are persisted separately.

This separation is deliberate. Renaming a worksheet must not reset JobSift's
delivery history or allow already-delivered jobs to be sent again.

## Inspect a client sheet

With Google service-account credentials available through Application Default
Credentials:

```bash
job-scout destination inspect \
  --sheet-url 'https://docs.google.com/spreadsheets/d/.../edit#gid=0'
```

JobSift reads metadata and the header row, then proposes mappings for common
client header names. Examples include:

| JobSift field | Recognized client headers |
| --- | --- |
| Job Title | Job Title, Title, Role, Position |
| Company Name | Company Name, Company, Employer |
| Job Link | Job Link, Link, URL, Job URL, Apply Link |
| Job Description | Job Description, Description, Details |
| Job Platform | Job Platform, Platform, Source |
| Status | Status, Application Status |
| Batch Prepared At | Batch Prepared At, Date Found, Date Added |

Only **Job Link** is required in Destination V1. Other JobSift fields are written
when the client has mapped columns for them.

If a client uses an unusual header, provide an explicit override:

```bash
job-scout destination inspect \
  --sheet-url 'https://docs.google.com/spreadsheets/d/.../edit#gid=0' \
  --map 'Job Link=Vacancy URL' \
  --map 'Job Title=Position Name'
```

Destination V1 requires the actual header to be row 1. A spreadsheet with multiple
tabs must be supplied with a `gid` in the link or an explicit `--worksheet`.

## Register and bind a campaign

```bash
job-scout destination register \
  --database jobs.sqlite3 \
  --client acme-client \
  --destination-id acme-jobs \
  --campaign-id acme-software-001 \
  --sheet-url 'https://docs.google.com/spreadsheets/d/.../edit#gid=0'
```

Registration performs a real metadata/read-access check, records the stable
worksheet ID, fingerprints the exact headers, stores the column mapping, and
binds the campaign.

The client must still grant **Editor** access. The Sheets API does not expose a
reliable read-only permission probe through the current delivery contract, so an
insufficient write permission fails closed on the first release and the same
frozen batch can be retried after access is corrected.

Use `destination register` again with the same destination ID to revalidate an
intentional header or tab-name change. Moving an active campaign to a different
physical worksheet is blocked; pause or complete that campaign first.

## Delivery safety

For managed delivery, a prepared batch freezes a `SheetDeliveryContract`.
Changing the registered mapping after preparation therefore cannot redirect that
already-reviewed batch.

Before every append, JobSift verifies:

1. the batch client owns the frozen contract;
2. the stable logical destination ID matches the batch;
3. Google's worksheet ID still exists;
4. the worksheet name has not changed since validation;
5. the exact header fingerprint still matches;
6. there is no unsafe internal blank row;
7. the sheet state still matches the batch's pre-write journal.

If any check fails, the batch is not silently redirected or rebuilt.

The optional client `Status` column is treated as operator-editable and is
excluded from publication recovery digests, matching the existing legacy-sheet
behavior.

## JobSift remains authoritative

Deleting or editing a row in a client's spreadsheet does **not** erase JobSift's
delivery history. Duplicate prevention remains in the database through the
client/destination delivery ledger.

The Sheet is an output surface only.

This prevents a client edit such as deleting yesterday's Reddit row from causing
JobSift to send that same practical job again.

## Client isolation

A physical Google worksheet can be registered to only one JobSift destination.
A destination belongs to one client. A campaign must belong to the same client as
its destination.

Only one campaign may be active on one destination at a time. This avoids
ambiguous unresolved releases and concurrent writes into the same client table.

## Live runner

Managed production runs resolve delivery by `JOBSIFT_CAMPAIGN_ID`. The runner
loads the campaign and destination from persistent state and freezes the resolved
contract into the batch.

The old `JOBSIFT_SHEET_ID` / `JOBSIFT_SHEET_TAB` path remains only as a
backward-compatible legacy path for the existing test sheet.

Do not put client spreadsheet links or IDs into public GitHub Actions workflow
inputs. This repository is public. Onboarding should happen through the CLI in a
trusted environment or, later, an authenticated operator/admin surface.

## What this does not do

This subsystem solves destination ownership, schema mapping, drift detection, and
safe publication. It does not yet turn the read-only development operator service
into a production client-onboarding UI, and it does not fan one workflow run out
across every active client campaign. Those are separate orchestration/auth tasks.
