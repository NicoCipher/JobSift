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

The next throughput step is the authoritative fan-in/persistence writer. Until that
exists and is proven idempotent, the parallel collector is not wired into Live
JobSift.

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
