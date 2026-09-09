# Historical operator job state

`job-scout history import` records prior operator job-link evidence without creating synthetic current postings. The ledger is client-scoped and stores each original URL, conservative normalized URL, optional supported source identity, workbook checksum, sheet, row, and status.

Every imported row, including `Applied`, `Not Applied`, and blank/unknown rows, represents a vacancy that was already surfaced. Delivery selection suppresses a current matched posting only for that same client when its exact source/board/provider identity or conservative normalized URL appears in the ledger. Collection, persistence, deterministic matching, and delivery-group behavior remain unchanged.

Run:

```sh
python -m job_scout history import --client taiwo --workbook /path/to/operator-history.xlsx --database jobs.sqlite3
```

The same workbook checksum and client combination is idempotent. Operator statuses are provenance, not matching decisions or a company blacklist.

An explicitly named blacklist sheet is imported into a separate provenance table. It does not affect matching or delivery until a separately approved blacklist policy exists.
