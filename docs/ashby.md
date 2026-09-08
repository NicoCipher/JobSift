# Ashby public collector

`AshbyCollector` implements the existing `JobCollector` protocol. Each CLI invocation
collects one target through the existing persistence, dedupe-v1, deterministic-v5
matching, client delivery history and five-column CSV pipeline.

Official contract reviewed 2026-09-05:
https://developers.ashbyhq.com/docs/public-job-posting-api

Public request: `GET https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=false`.
No authenticated ATS API, candidate endpoints, HTML scraping, or applications are used.
Default timeout is 15 seconds with two bounded connection retries, as in Greenhouse.
HTTP rate-limit responses are classified, not retried in a polling loop.

## Identity and source evidence

Use a valid nonempty provider ID token when supplied. Otherwise accept a UUID in the
board-scoped `jobs.ashbyhq.com/{board}/{uuid}` job URL. Unknown future non-UUID URL
formats without provider IDs are quarantined, never identified from titles or hashes.
Internal identity is UUID5 over `ashby:{board}:{source_job_id}`, matching the existing
source/board/provider convention. Provider ID and identity mechanism are recorded.

Both job and application URLs are retained and canonicalized by the existing URL
rules. The normalized job URL is canonical. Plain description is preferred unchanged
and untruncated; existing HTML cleanup supplies the fallback. Original HTML is kept.
`publishedAt` is `posted_at`; `updated_at` remains unknown.

Country evidence comes from the primary structured address, secondary structured
addresses, and per-location text fallback. Primary nested postal addresses and both
documented flat and observed nested secondary addresses are supported. Known country
aliases/codes are canonicalized explicitly. Other named country values are retained
as direct source evidence; unknown short codes and recognized regional labels are not
expanded. Text fallback uses the existing bounded normalizer. Structured evidence
wins over conflicting text for the same location. Multiple countries remain a set.
Raw addresses and primary/secondary location names remain in the metadata allowlist.

`workplaceType` wins over `isRemote`: Remote/Hybrid/OnSite map directly. An unknown
new workplace enum stays unknown. Only absent workplace type permits `isRemote=true`
to establish remote; absent flags permit existing conservative textual classification.
`isRemote=false` alone does not distinguish hybrid from on-site.

FullTime/PartTime/Contract/Temporary/Intern map to the corresponding canonical types.
Other values remain unknown and are preserved in metadata. Department maps directly;
team stays a separate metadata fact. Parsed optional source fields are explicitly
allowlisted; unknown fields, compensation and candidate/application objects are dropped.
Fingerprinting covers descriptions and normalized/source evidence including country,
work mode, URLs, team, and publication changes; it does not manufacture update timestamps.

## Failure and skipped-posting semantics

404 = invalid target; 429 = rate limited; 401 = authentication failure. 403 is rate
limited only with retry/throttling evidence, otherwise forbidden. Other unsuccessful
HTTP statuses, including 5xx, are provider errors. Transport errors are network
failures. Invalid top-level JSON/schema is a parse failure. Bad individual records
produce partial results with valid jobs preserved and validation errors without raw
input dumps. Explicitly unlisted postings are skipped before normal discoverable-job
validation. `last_counts` reports received, normalized, quarantined and skipped counts
without modifying `CollectionResult`. A valid empty board remains successful.

## Reproducible evaluation

The frozen September 3 cohort is `validation/ashby_production_v1/cohort.csv`. Small
preserved ClickHouse, Supabase and Sentry payloads are test fixtures. The complete
archive is external; cohort hashes identify each original board payload.

Run `python -m validation.ashby_production_v1.evaluate --phase offline` against those
external files. It uses a mocked HTTP transport with the production collector, not
the previous ad-hoc normalizer. Fresh board databases preserve all normalized postings.
The offline gate requires 797 raw/normalized postings, zero quarantines and 15 successful
boards. It is an ingestion gate, not a demand to reproduce old normalization mistakes.

After publication, run the same driver with `--phase live --commit <published SHA>`.
The manifest freezes cohort, commit, code hashes, brief hash, versions and timestamp
before any request. Raw responses and independent databases/CSVs are saved per board.
Both phases refuse to overwrite prior results. This is bounded evaluation tooling.
The `job-scout source --plan ...` command provides bounded multi-board operator
runs; scheduling remains intentionally out of scope.

The offline run delivered one ClickHouse federal support role. Compared with the old
ad-hoc replay, explicit remote source evidence changes that role from review to strong;
explicit Hybrid changes another ClickHouse role from review to reject. All matcher and
dedupe source bytes remain frozen. Citizenship restrictions remain a human decision
under this operator-style brief.
