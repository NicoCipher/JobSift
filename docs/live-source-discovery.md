# Live Source Discovery + Admission v1

JobSift no longer treats the frozen historical target universe as the only way an
employer ATS board can become production-eligible.

## Safety boundary

Discovery evidence is **not** delivery evidence and is **not** sufficient for
production admission.

The v1 pipeline is:

1. Read one bounded CDX page per supported ATS query from the latest Common Crawl.
2. Parse only URLs that match JobSift's existing canonical provider-coordinate
   contracts.
3. Persist immutable URL-level discovery evidence in Turso.
4. Health-check candidates directly against the provider's public API.
5. Admit only targets whose latest direct provider health result is `active`.
6. Expire runtime admission when active health evidence is older than 48 hours.
7. Merge current live admissions with the frozen production registry during the
   hourly refresh planning step.
8. Persist the exact merged parent-registry snapshot with the refresh plan so
   fan-in, coverage reporting, and audit use the same source universe.

Common Crawl is therefore used only for broad target discovery. Job data still
comes from the canonical provider adapters, and all existing freshness, matching,
dedupe, history, retention, client quota, pause/review, and destination controls
remain unchanged.

## Supported discovery patterns

- Greenhouse: `job-boards.greenhouse.io` and EU host
- Ashby: `jobs.ashbyhq.com`
- Lever: global and EU hosts
- Workday: `*.myworkdayjobs.com`
- SmartRecruiters: `jobs.smartrecruiters.com`

The Common Crawl client discovers the latest crawl dynamically from
`collinfo.json`, stores one page cursor per query, and advances by one compressed
index page each run using a deterministic co-prime stride. This spreads discovery
across very large provider indexes instead of repeatedly starting at the lexical
front of every new crawl. Requests are serialized and delayed to avoid hammering
the public CDX service.

## Admission state

Turso stores:

- canonical discovered target coordinates,
- first/last discovery timestamps,
- immutable Common Crawl URL evidence,
- per-query crawl/page cursors,
- immutable provider health observations,
- latest health classification,
- current admission state,
- next health-check time.

New targets are checked before previously checked targets. Recheck cadence is
classification-aware:

- active / valid empty: 24 hours (due admitted targets are checked before new candidates)
- transient / rate limited / malformed: 6 hours
- restricted: 3 days
- invalid / unprocessable: 7 days

Only `active` targets are runtime-admitted.

## Scheduling

`.github/workflows/live-source-discovery.yml` runs every three hours and can also be
manually dispatched. It performs bounded discovery and at most 300 provider health
checks per run.

The hourly inventory refresh reads current admissions from the same Turso
database. If the discovery workflow is unavailable, previously admitted targets
automatically stop entering runtime after the 48-hour health-evidence window;
the frozen registry remains available.

## Operator commands

Run one discovery/admission cycle:

```bash
python -m job_scout.source_discovery run \
  --database /tmp/jobsift-source-discovery.sqlite3 \
  --base-registry config/source_registries/production_active_v1.json \
  --max-health-checks 200 \
  --output source-discovery-report.json
```

Inspect current discovery/admission state without external requests:

```bash
python -m job_scout.source_discovery status \
  --database /tmp/jobsift-source-discovery.sqlite3 \
  --base-registry config/source_registries/production_active_v1.json
```

## v1 limitations

This version expands the target universe; it does not claim that the existing
hourly provider cohort sizes can cover an arbitrarily large discovered registry
inside 24 hours. Coverage remains measured by the existing refresh coverage
report. Provider-specific scaling should be promoted only from measured runtime
and fresh-yield evidence.
