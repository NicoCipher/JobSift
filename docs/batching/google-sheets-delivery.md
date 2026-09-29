# Google Sheets batch delivery

Daily Batch can release an already prepared batch to an explicitly configured
Google Sheet. It does not source jobs, infer a client brief, or create a batch.

## Destination contract

Use `gsheet://SPREADSHEET_ID/TAB_NAME` as the `DailyBatchRequest.destination`.
Build it with `sheet_destination(spreadsheet_id, tab)` to encode the tab and use
the same identity for client/destination prior-delivery suppression. Configure
the authorized spreadsheet ID and tab in operator deployment settings, not in
source control. Do not reuse an example sheet. Bind the destination to
an explicit client ID and authoritative evidence scope when preparing a batch.

The tab's first nine columns must be exactly:

`Job Title, Company Name, Job Link, Job Description, Job Platform, Status, Batch ID, Batch Prepared At, Job ID`

The publisher appends frozen job details with `Status` blank. Operators own that
column and may change it without breaking recovery. The batch ID and job ID
identify a delivered row. `Batch Prepared At` is the persisted assembly time in
UTC, not a claim about the time the Google API accepted the write.

## Authentication and release

Install `job-scout[sheets]`, configure Google Application Default Credentials
for a dedicated runtime identity with Sheets scope, and share this spreadsheet
with that identity as an editor. Do not commit credentials. ChatGPT's connected
Google Drive access does not supply credentials to the Python runtime.

Review a prepared batch with `job-scout batch review --database jobs.sqlite3
--batch-id ID`. The review opens SQLite read-only and does not contact Google.
Release with `job-scout batch release --database jobs.sqlite3 --batch-id ID
--confirm-batch-id ID`. A failed release exits nonzero and retains the frozen
batch and journal for inspection.

## Recovery boundary

The publisher records before/after digests of controlled sheet values before
writing. It uses a RAW append, then reads back the complete sheet. If the append
committed but its response was lost, retry recognizes the after image and marks
the batch delivered without adding more rows. If the sheet matches neither
journal image, delivery fails closed for operator reconciliation. A separate
database or a non-cooperating writer is not serialized by the batch journal;
use one authoritative database per destination and avoid simultaneous external
row insertion. Editing Status on existing rows is allowed.

There is no live link delivery until a real client brief, authoritative sourcing
scope, runtime credentials, and a persistent database are connected and tested.
