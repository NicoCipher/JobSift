# Workday public CXS collector

`WorkdayCollector` is the third JobSift source family. It uses only a configured
public Workday CXS board:

- `POST https://{host}/wday/cxs/{tenant}/{site}/jobs`
- `GET https://{host}/wday/cxs/{tenant}/{site}{externalPath}`

Each target must provide its host, tenant and site explicitly. JobSift does not
derive those coordinates from a human-facing URL. The CLI accepts
`--workday-host`, `--workday-tenant`, and `--workday-site`; source-board identity
is the stable `host:tenant:site` tuple.

The search request is serialized, uses `limit=20`, and takes its stopping total
from page zero. It never requests `offset >= first_page_total`, because observed
boards may wrap to page one at that offset. Repeated page signatures, malformed
paths, missing details and individual normalization failures return a `partial`
collection instead of asserting completeness.

## Capped-board coverage

A broad result below 2,000 is normally paginated. A broad result of exactly 2,000
is a lower bound unless its current `jobFamilyGroup` facet contract is safe: at
least two unique facet IDs, every advertised count below 2,000, and an advertised
sum greater than 2,000. The collector retrieves every such partition, verifies its
first-page total against the advertised count, resolves public detail records, and
unions them by `jobReqId`. Any partition failure or cross-partition provider-ID
overlap leaves the collection `partial`; the already retrieved postings remain
available as a bounded lower-bound result.

The frozen NVIDIA validation proved this recovery contract on 2026-09-05: the
broad query contained 2,000 IDs, the 15-partition union contained 2,695 distinct
IDs, every broad ID was in the union, and there were no pagination anomalies or
cross-partition ID overlaps. The large checkpoint JSONL state remains local; the
committed contract retains its small summaries and provenance.

## Field mapping

`jobReqId` is the provider ID. `externalUrl` is the canonical job URL; Workday CXS
does not provide a distinct application URL, so `apply_url` stays empty. HTML
`jobDescription`, title, structured country, location/additional locations,
`remoteType`, `timeType`, and date-only `startDate` are retained conservatively.
Only documented/observed Remote, Hybrid and In-Office values map to work mode;
only Full time and Part time map to employment type. `postedOn` is relative text,
so it remains source metadata rather than an invented timestamp. `updated_at` and
department remain unknown.

The frozen live cohort is `validation/workday_contract_v1/cohort.csv`, selected
from repeated real operator links before vacancy inspection. Run its post-publish
audit with:

```bash
python -m validation.workday_production_v1.evaluate --commit <published-SHA>
```

It writes a freeze manifest before any request and stores only per-board metrics,
CSVs and local SQLite audit state; it does not save provider response bodies.
For bounded multi-board operator runs, use `job-scout source --plan ...`;
scheduling remains intentionally out of scope.
