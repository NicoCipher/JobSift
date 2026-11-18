# Inventory observation compaction and expiry plan

## Decision

Do not delete the 9,234 superseded `inventory_target_observations` rows yet. The current coverage-report path uses this ledger for historical replay, including an as-of fallback when the compact `inventory_target_coverage_state` contains observations newer than the report's generated time. The current per-target state (first/previous/last timestamp, count, last run) is not sufficient to reconstruct every historical as-of result or revisit percentile.

This document defines a safe migration design. It does not authorize production deletion.

## Protected records (never part of this expiry)

- `job_identity_ledger`: protects provider identity and cross-source deduplication.
- `delivery_keys`, delivery groups, group-delivery records, exports, and batch receipts/items: protect idempotency, delivery history, and recovery from uncertain external writes.
- The latest `source_discovery_evidence` row and latest `source_discovery_health_evidence` row per target, plus `source_discovery_targets`: protect current source discovery and verification state.
- `inventory_target_coverage_state`: current per-target coverage anchor.
- Running inventory runs and any run receipts needed for idempotency/replay.
- The latest observation per target, regardless of age.

No retention job may cascade from these protected tables or infer eligibility from age alone.

## Proposed compact representation

Add an append/upsert-only `inventory_observation_daily_aggregates` keyed by UTC date and target identity (with source, schema version, observation count, first/last completion timestamps, status counts, and sums for raw postings, normalized jobs, trustworthy timestamps, and <=24h postings).

For exact historical replay, each aggregate must also retain a compact, ordered, lossless sequence of observation event timestamps and the metric/status values required by the report. Delta-encode timestamps relative to the bucket start and use bounded integer encodings. Do not rely only on daily min/max or approximate quantile sketches: an as-of replay can end inside a day, and current reporting derives per-target revisit intervals and p50/p95 runtime/revisit statistics. A histogram/sketch may be added as a fast path, but the lossless event sequence remains the correctness source until tests prove an equivalent exact query.

Retain a raw hot window (initial proposal: 30 days) for straightforward operational diagnosis. Aggregates cover older complete UTC days. Keep the latest raw observation per target outside that window as a replay anchor. Preserve run receipts and immutable membership counts separately from observation compaction.

## Migration and read-path sequence

1. Add aggregate schema and deterministic backfill in bounded batches, without deleting or rewriting source observations.
2. Make report generation read raw events for the hot window and lossless aggregates for older complete buckets; merge and sort events before deriving previous-observation timestamps, revisit intervals, coverage, and percentiles.
3. For a cutoff that falls inside an aggregate bucket, decode the event sequence and filter by the exact timestamp. Never use a whole-day count/min/max as a substitute.
4. Add parity tests comparing old raw-only output with aggregate-backed output for normal reports, replayed older runs, same-timestamp tie cases, failed/partial targets, empty targets, and cutoffs at bucket boundaries.
5. Run shadow comparisons against production reads and record differences. Require zero unexplained differences for at least one full reporting/retention cycle.
6. Only then add a dry-run expiry report listing exact candidate row IDs/counts and proving every event is represented in a validated aggregate. Require explicit approval before enabling deletion.
7. Delete only superseded raw rows older than the hot window whose aggregate checksum, event count, and report parity have been verified. Keep latest-per-target raw rows and all protected records above.

## Safety invariants and rollback

- Aggregate writes and source observation writes must commit atomically.
- A unique source-event key (run_id, target_identity) prevents double counting on retries.
- Backfill is idempotent and verifies per-bucket count/checksum before marking complete.
- No deletion when a bucket is missing, incomplete, checksum-mismatched, or has an unsupported schema version.
- A rollback disables aggregate reads and returns to raw observations; therefore source rows remain untouched until the final separately approved expiry phase.
- Monitor database bytes, WAL/write volume, query time, and report parity before and after each stage.

## Current known data

The earlier read-only production audit counted 9,234 superseded observations older than 30 days and 883 latest-per-target observations in the same age range. These are audit counts, not an instruction to delete. Recount immediately before any future migration because production changes continuously.
