---
name: jobsift-throughput
description: JobSift-specific throughput and scaling workflow. Use for 2k/day capacity, 20k inventory, source discovery, ATS collection, Workday, scheduling, fan-in, persistence, matching, delivery, database bottlenecks, or performance regressions.
metadata:
  origin: JobSift
---

# JobSift Throughput

Optimize the measured bottleneck without weakening JobSift's product contract.

## First isolate the stage

Do not treat "JobSift is slow" as one problem. Measure the relevant path:

```text
discovery -> ATS collection -> normalization -> fan-in/persistence
          -> SearchBrief evaluation -> dedupe/quota -> Sheet delivery
```

Read only the implementation, tests, workflow step, and evidence for the suspected stage. The merged 20k persist-and-deliver path is an existing baseline; do not rebuild it from first principles.

## Workflow

1. Establish a reproducible baseline from current production-like evidence.
2. Name one bottleneck hypothesis and the metric that would disprove it.
3. Make the smallest bounded change that can improve that metric.
4. Benchmark before/after under comparable inputs.
5. Verify correctness invariants and failure/retry behavior.
6. Promote only a measured improvement; otherwise revert or keep it experimental.

Prefer batching, bounded concurrency, checkpointing, idempotent writes, source-yield scheduling, and moving work away from constrained shared resources. Preserve the authoritative persistence boundary unless evidence supports an architectural change.

## Required accounting

Report the metrics relevant to the changed stage, not vanity totals:

- targets attempted/succeeded/failed;
- raw postings and trustworthy-age postings;
- postings <=24h old;
- full SearchBrief matches and distinct eligible employers;
- duplicates/history/policy suppressions;
- prepared or delivered count and shortfall;
- stage runtime plus p50/p95 where meaningful;
- persistence/query/write counts when the database is implicated;
- errors, retries, incomplete artifacts, and correctness gate.

Raw ATS volume alone never proves client capacity.

## Guardrails

- Never relax freshness, matching, dedupe, history, quotas, or source credibility for throughput.
- Never hide failed or incomplete shards to improve a metric.
- Do not increase provider concurrency blindly; keep it bounded and evidence-driven.
- Do not create competing production writers merely to make a benchmark faster.
- Separate discovery breadth from deliverable client yield.
