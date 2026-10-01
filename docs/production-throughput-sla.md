# Production throughput and freshness SLA

JobSift's production goal is not a small curated pilot. The system is being built
to sustain high-volume sourcing while keeping job quality and evidence explicit.

## Throughput target

- Minimum target: **150 deliverable jobs per day**.
- Weekly target: **1,050+ deliverable jobs per rolling 7 days**.
- These are delivery targets, not raw ATS posting counts.
- Capacity is not considered proven until a production-like benchmark sustains the
  target for 7 consecutive days without weakening client rules.

A job contributes to the throughput target only when it passes the client's full
SearchBrief, freshness rule, dedupe/history rules, and employer-delivery policy.

## Freshness

For the current remote-US software client:

- a posting must have trustworthy posting-time evidence;
- the posting must be no more than **24 hours old at evaluation time**;
- unknown posting age fails closed and is not automatically deliverable;
- sorting newer jobs first is not a substitute for the freshness gate.

Provider evidence currently used:

- Greenhouse: `first_published`;
- Ashby: `publishedAt`;
- Lever: public `createdAt` Unix epoch milliseconds;
- Workday: normalized CXS posting-date evidence.

## Active inventory retention

Full active posting payloads are retained for at most **72 hours**.

After the retention window:

- stale, undelivered jobs are deleted from active storage;
- large descriptions, HTML and raw provider metadata are stripped from records
  that must remain because they were delivered or are part of immutable batch
  history;
- a minimal identity ledger may remain for duplicate prevention, delivery history
  and auditability;
- detailed candidate rows for old delivered batches expire while batch summary
  counts and selected rows remain.

The minimal ledger is intentionally not active job inventory. Removing it would
allow old delivered jobs to re-enter later as false "new" opportunities.

## Source scale

The historical target universe contains 2,287 supported ATS targets. The last full
health validation (2026-09-09) classified 2,044 as active and observed roughly
235,622 raw current posting records.

That is source-breadth evidence, not proof of 150 fresh client matches per day.
Targets may be non-US employers, may have no relevant software role, or may have
no posting younger than 24 hours.

Production source promotion is now explicit rather than implicit. The checked
`production-source-registry-v1` artifact contains only the 2,044 targets that the
complete 2026-09-09 health run classified as active. It carries the canonical
target-universe Git blob identity and health-manifest SHA so the approval evidence
is auditable. A future registry version must be rebuilt from a complete health run;
missing, duplicate, non-active, or source-mismatched evidence fails closed.

This registry is a collection allowlist. It does **not** assert that every approved
target is relevant to every client or can contribute a fresh match today.

## Collection architecture for scale

The live production path now separates source collection from client evaluation:

1. collect an ATS target once into a shared inventory run;
2. persist normalized jobs and run membership without exporting them;
3. evaluate that same inventory run independently for each SearchBrief/client;
4. apply client delivery policy and publish only at the batch layer.

This removes the old local-CSV delivery side effect from the live runner and means
a second client can be evaluated against the same collected inventory without a
second ATS fetch. The legacy `job-scout source` command retains its original
collect/match/CSV behavior for compatibility.

The current seven-employer collection is still sequential. Shared inventory is a
necessary boundary for scale, but it is **not** evidence that 2,044 targets can be
processed within the production time budget.

Provider-aware deterministic sharding is now defined over that registry. Shard
counts remain an explicit runtime/benchmark input rather than an invented capacity
claim: targets are isolated by provider, deterministically ordered, distributed
once across that provider's shards, and validated for duplicate-free coverage.

The shard-worker artifact contract is now explicit: workers perform provider reads
only and emit normalized, content-fingerprinted artifacts with per-target status,
raw posting counts, trustworthy timestamp counts, <=24h-at-collection metrics, and
target runtime p50/p95. A complete artifact set must match the exact registry and
shard-manifest provenance and cover every expected shard exactly once.

The authoritative fan-in/persistence boundary now validates the complete artifact
set before creating an inventory run, verifies each normalized job belongs to its
approved target, deduplicates provider identities before persistence, and derives a
deterministic run receipt from the exact artifact set. Replaying the same completed
artifact set returns the existing run instead of re-persisting it. A crashed
`running` fan-in can safely retry through idempotent job upserts and run
membership inserts.

The parallel collector is still not wired into Live JobSift. Workflow parallelism
comes only after this fan-in path passes CI/review and a bounded benchmark chooses
provider shard counts from measured runtime rather than assumption.

At scale:

1. maintain a versioned registry of production-approved source targets;
2. split targets into deterministic provider-aware shards;
3. collect shards in parallel without writing to Turso from every worker;
4. upload normalized shard artifacts;
5. fan in to one aggregation job;
6. persist/dedupe/match through one authoritative writer;
7. apply the 24-hour freshness gate and client delivery policies;
8. freeze/release the client batch through the existing delivery journal.

This avoids concurrent Turso Sync writers and prevents a single 2,000-target
sequential GitHub Actions job from becoming the bottleneck.

## Metrics required on every production-scale run

At minimum record:

- source targets attempted/succeeded/failed;
- raw postings received;
- postings with trustworthy age evidence;
- postings <=24h old;
- full SearchBrief matches;
- distinct eligible employers;
- practical duplicates suppressed;
- employer-policy suppressions;
- selected/delivered count;
- shortfall;
- p50/p95 source and total runtime.

A run with 150 rows is not successful if the rows are old, duplicated, or produced
by weakening the client's requirements.


## Bounded parallel benchmark

Before production parallelism is connected to Turso or Google Sheets, JobSift uses
the manual `Bounded Source Benchmark` workflow.

The workflow selects a deterministic subset only from the production-approved
registry, builds provider-isolated shards, runs those shards as parallel DB-free
workers, validates the exact complete artifact set, persists it to a temporary
local SQLite database, and evaluates the shared inventory against the current
remote-US software SearchBrief. The local database and JSON report are uploaded as
short-lived benchmark evidence.

The default first pass is intentionally small: 10 Greenhouse, 10 Ashby, 5 Workday,
and 10 Lever targets across two shards per provider (35 targets / 8 workers). The
benchmark runtime enforces a 100-target ceiling so this workflow cannot
accidentally become the full production collector.

The benchmark does **not** publish jobs, does not receive Turso credentials, and
does not receive Google Sheets credentials. It records collection throughput,
timestamp/freshness evidence, full SearchBrief evaluation counts, distinct matched
employers, and distinct practical delivery groups. These results are used to
choose provider shard counts for the next larger benchmark rather than treating
the default shard counts as a capacity claim.


### First bounded benchmark evidence

GitHub Actions run `36782209908` executed the first 35-target / 8-worker
benchmark from the production-approved registry. Seven of eight shard artifacts
completed. The remaining shard, `workday-001`, hit the 30-minute job guard and
did not publish an artifact, so authoritative fan-in rejected the seven-artifact
set as incomplete. No benchmark inventory was accepted and no delivery occurred.

The completed non-Workday targets (30/30 successful) produced 815 normalized/raw
postings with trustworthy timestamps, of which 14 were at most 24 hours old at
collection: Greenhouse 526 / 9 fresh, Ashby 219 / 4 fresh, Lever 70 / 1 fresh.

The completed Workday shard covered three targets and produced 613 postings, 28
at most 24 hours old. Its target runtimes were approximately 340 seconds for 330
Allegion postings, 226 seconds for 269 Yale postings, and 13 seconds for 14
Bullhorn postings. The timed-out Workday shard included the production-approved
Genuine Parts board with health evidence of 2,000 current postings. This exposed
serial Workday detail reads as the first measured scaling bottleneck.

The next benchmark keeps Workday search pagination and coverage validation
serial but tests a bounded detail-read concurrency of four. The normal
`WorkdayCollector` default remains one; benchmark concurrency is explicitly
frozen into the benchmark plan, capped at eight, and therefore cannot silently
change ordinary collector behavior.


### Successful 35-target benchmark with bounded Workday concurrency

GitHub Actions run `36807915457` repeated the same deterministic 35-target
sample with Workday detail concurrency frozen at four. All 35 targets completed,
all eight shard artifacts were present, authoritative fan-in succeeded, and no
Turso or Google Sheets credentials were used.

The run collected 4,199 unique normalized postings. All had trustworthy posting
timestamps, but only 26 were at most 24 hours old at collection. Client
evaluation found 23 semantic remote-US software matches before freshness; all 23
were older than 24 hours, so the final freshness-eligible match count was zero.
The 26 fresh postings all failed the title-target rule. This sample therefore
does **not** support a 150-deliverable-jobs/day capacity claim.

Workday concurrency materially improved the measured bottleneck. On the same
three-target shard, Allegion fell from about 340 seconds to 94 seconds, Yale from
about 226 seconds to 64 seconds, and Bullhorn from about 13 seconds to 4 seconds.
The previously timed-out Workday shard also completed: the Genuine Parts board
recovered 2,750 postings (showing that its health count of 2,000 was a provider
cap, not an exact inventory count) and completed in about 986 seconds with zero
collector errors.

The immutable aggregate evidence for this run is stored under
`validation/bounded_benchmark_v1/run_36807915457/summary.json`. The next
capacity test should expand the deterministic sample while increasing Workday
shard count to reflect measured target-size skew; it must continue to report
fresh full-brief matches and distinct eligible employers rather than raw ATS
volume.
