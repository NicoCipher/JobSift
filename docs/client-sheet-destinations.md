# Client-owned Google Sheet destinations

JobSift treats a client's Google Sheet as a delivery surface, not as the system of
record.

## Ownership model

The client owns the workbook. JobSift owns:

- source/match evidence;
- dedupe and delivery history;
- campaign/batch state;
- logical destination identity;
- the mapping from JobSift fields to client columns.

Deleting or editing a delivered row in the client's Sheet does not make the job
eligible for redelivery. Duplicate protection is driven by JobSift's own ledger.

## Client onboarding

1. The client provides a Google Sheet URL and the worksheet/tab to use.
2. The client shares the workbook with the JobSift service account as Editor:

   `jobsift-sheets-publisher@jobsift-510120.iam.gserviceaccount.com`

3. The operator registers a stable JobSift destination ID and a column mapping.
4. JobSift reads the workbook metadata and header row.
5. Registration stores:
   - client ID;
   - logical destination ID;
   - spreadsheet ID;
   - Google's stable numeric `sheetId`;
   - expected tab name;
   - exact header fingerprint;
   - column mapping;
   - destination configuration fingerprint.
6. A prepared batch freezes the destination ID and configuration fingerprint.
7. Release re-verifies the current `sheetId`, tab name, and exact header before
   any append.

No test row is written during registration.

## Phone-friendly registration

Use **Actions -> Register Client Sheet -> Run workflow**.

Required inputs:

- `client_id`
- `destination_id` such as `primary-jobs`
- display name
- Google Sheet URL
- exact tab name
- a JSON mapping

Example:

```json
{
  "Job Link": "URL",
  "Job Title": "Role",
  "Company Name": "Company",
  "Status": "Application Status"
}
```

Only `Job Link` is mandatory. Other supported fields are:

- Job Title
- Company Name
- Job Link
- Job Description
- Job Platform
- Status
- Batch ID
- Batch Prepared At
- Job ID

The workflow uses the existing Google service-account and Turso secrets. It stores
the destination in Turso; it does not put client Sheet URLs into repository code.

## Arbitrary client layouts

A client may have:

```text
Role | Company | URL | Recruiter | Notes | Application Status
```

JobSift can map:

```text
Job Title    -> Role
Company Name -> Company
Job Link     -> URL
Status       -> Application Status
```

`Recruiter` and `Notes` remain client-owned and are left blank on newly appended
rows unless the client fills them.

During uncertain-append recovery, edits to unmapped columns are ignored. A mapped
`Status` column is also intentionally mutable. Changes to headers, row structure,
JobSift-owned mapped values, the registered tab, or destination configuration fail
closed and require reconciliation.

## Stable logical identity

Delivery history is keyed by a logical URI such as:

```text
client-sheet://primary-jobs
```

It is not keyed directly by `spreadsheet_id + tab name`.

This means a controlled destination re-registration can change physical Sheet
configuration without erasing prior delivery history for that client destination.

## Legacy migration

The existing global `JOBSIFT_SHEET_ID` / `JOBSIFT_SHEET_TAB` path remains
supported while current test/production destinations are migrated.

Once a client destination is registered, set:

```text
JOBSIFT_DESTINATION_ID=<destination_id>
```

The live runner then resolves the client's destination from Turso. Physical Sheet
coordinates no longer need to be the authoritative delivery identity.

Validation-only runs remain isolated and never write to client Sheets.

## Failure behavior

Release is blocked if:

- the destination is missing or disabled;
- the registered Google `sheetId` disappears;
- the tab was renamed;
- the header row changed;
- a mapped target header is missing/ambiguous;
- the destination was reconfigured after batch preparation;
- the Sheet changed in a way that conflicts with the delivery journal;
- append outcome cannot be verified.

A failed release remains recoverable through the existing batch journal. JobSift
never silently changes the destination or column mapping to make a write succeed.
