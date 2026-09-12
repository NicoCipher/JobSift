# Daily Batch V1

Daily Batch V1 assembles a global, explicit set of already acquired and evaluated
postings. It does not collect, invoke the matcher, weaken a SearchBrief, or enforce
historical blacklist notes. The existing single-target `run_pipeline` remains
unchanged. No API, frontend, auth, or CLI is introduced.

## Entry points and evidence

- [Domain contract](../../job_scout/domain/daily_batch.py): `DailyBatchRequest`,
  `DailyBatchResult`, `DailyBatchCounts`, `DailyBatchItem`, `BatchConflict`.
- [Orchestration](../../job_scout/orchestration/daily_batch.py):
  `prepare_daily_batch(repository=..., request=...)` and
  `finalize_daily_batch(repository=..., batch_id=...)`.
- [Storage](../../job_scout/storage/daily_batches.py): `DailyBatchStore`, including
  `evidence_digest(client_id, job_ids)` and `get(batch_id)`.
- [CSV finalization](../../job_scout/export/batch_csv.py).

A request requires stable client ID, positive integer quota (booleans and strings
are rejected), explicit destination, idempotency key, evidence scope ID, evaluation
ID, unique candidate job IDs, and a SHA-256 digest of their persisted job/match
rows. Capture that digest with `evidence_digest`, then prepare. Missing matches or
a digest mismatch fail closed. Candidate IDs are sorted before hashing and storage.
Acquisition/evaluation must finish before this boundary; unrelated database rows
are never silently added to the candidate set.

The legacy `job_matches` table contains current mutable evaluations, not an
immutable run-to-brief registry. The caller must supply an authoritative scope and
evaluation identity. The digest verifies the exact current rows, not an asserted
association with a particular brief. Optional `brief_revision_id` and
`brief_sha256` must be supplied together and are caller-declared provenance; leave
both null when that linkage is unavailable. Never infer a V2 association from a
client name. `taiwo_operator_sourcing_v1` remains the stable client ID across V1/V2
sourcing revisions. The CLI is deferred until authoritative scope selection can
be exposed without implying that the legacy database can reconstruct this linkage.

Completeness is explicitly `complete`, `partial`, or `unknown` (default).
`source_failures` is preserved verbatim as supplied. A shortfall describes the
supplied evidence scope, not the total market or vacancy closure.

## Selection and accounting

Eligibility is exactly `strong_match` or `possible_match`; `needs_review` and
`reject` never fill quota. Fresh means delivery-fresh for this client/destination,
not newly discovered, recently posted, or a particular posting lifecycle.

Reuse persisted practical groups and the existing historical identity/URL
predicate. Historical surfacing of any persisted group member suppresses the group,
including a member outside the candidate set. Applied, Not Applied, and unknown
statuses all suppress. Historical suppression takes precedence over prior delivery,
so those categories are mutually exclusive. Prior delivery is scoped to
client + destination + group. Destination paths are resolved as in `run_pipeline`.

Group eligible postings using existing persisted group IDs. Remove historical and
prior-delivery groups, select the minimum existing `representative_key` among the
remaining eligible in-scope members, sort by persistent group ID, and take the
first Q groups. Group IDs are existing deterministic identities, not random IDs
created for selection. The representative key prefers direct application URLs,
then richer descriptions/evidence and existing timestamp tie-breaks, followed by
source/board/posting/ID. No new ranking or dedupe policy is introduced.

Counts use these denominators:

- `candidate_postings = match_eligible_postings + needs_review_postings + rejected_postings`.
- `match_eligible_postings = match_eligible_groups + duplicate_postings_collapsed`.
  Duplicate postings are counted across all eligible groups before suppression.
- `match_eligible_groups = historically_suppressed_groups + previously_delivered_groups + fresh_eligible_groups`.
- `selected_groups = selected_count = min(requested_quota, fresh_eligible_groups)`.
- `shortfall = requested_quota - selected_count`, always nonnegative.

Thus grouping before suppression retains equivalent quota semantics: neither
suppressed groups nor duplicate postings consume quota. Counts and item ordinals
are validated on result load. Requested quota is in `result.request`; all counts
are assembly-time facts, not claims about successful export. For requested 150 and
87 fresh eligible groups, selected is 87 and shortfall is 63. No frozen baseline
number is used by production selection.

## Persistence and replay

Opening `DailyBatchStore` initializes the additive schema, which creates `daily_batches`, `daily_batch_items`, and
`daily_batch_candidates`. Existing jobs, matches, exports, groups, and historical
evidence are retained. Batch columns normalize identity, quota, selected count,
shortfall, status, timestamps, and export journal hashes; request/count structures
are JSON. Items retain ordinal, historical group ID, representative posting ID,
evidence digest, matcher version, and the frozen five-column export row. Candidate
rows retain evidence digest, group, decision, and disposition. Whole job payloads
are not copied. Dedupe version is recorded on the batch.

Batch group IDs intentionally have no foreign key to live `delivery_groups`:
existing dedupe can merge/delete a group, while batch provenance must remain
immutable. Posting references retain foreign keys. Finalization resolves the
current group for delivery history.

Same client + resolved destination + idempotency key returns the persisted batch,
including its original selection, even if source evidence later changes. Any
incompatible request field raises `BatchConflict`. A new explicit key creates a
separate request. Preparing does not reserve groups or mark delivery. Competing
prepared batches are rechecked at finalization; an obsolete selection fails rather
than silently substituting other groups.

Statuses are `prepared`, `failed`, and `delivered`. `failed` retains selection,
shortfall, journal, and error for inspection/retry. `delivered_at` is set only when
all file work and delivery records complete. A failed batch may already have a
complete CSV image after an ambiguous failure; `failed` does not assert no rows
were written. A process interruption can leave `prepared` with a pending journal.

## File/DB recovery

CSV columns remain exactly Job Title, Company Name, Job Link, Job Description,
Job Platform. The existing export-row builder uses provider `apply_url`, otherwise
canonical vacancy URL; no URL is synthesized. Existing CSV bytes are retained as
a prefix; malformed headers/row widths fail before writing.

Finalization uses a stable sibling POSIX advisory lock and two SQLite transactions:

1. Recheck selected evidence, eligibility, historical/prior suppression and group
   uniqueness. Persist expected whole-file before/after SHA-256 hashes and commit.
2. Under a database write transaction, inspect the destination. If it matches
   before, recheck selection, write the complete image to a same-directory temporary
   file, fsync it, atomically replace the destination, and fsync the directory.
   If it already matches after, fsync and reconcile without appending.
3. Record existing group-delivery/export history and mark delivered in the same
   database transaction, after durable file publication.

Failure before replacement never marks delivered. Failure after replacement but
before DB commit leaves a recoverable journal; retry recognizes the exact image
and records the original batch without duplicate rows or a new selection. Once
that image exists, recovery records what was actually exported even if mutable
match evidence has subsequently changed. An already delivered retry returns its
result without rewriting or repairing a later externally edited CSV.

Only one unresolved journal per destination is allowed. Other batches fail until
that journal is reconciled. If the file matches neither hash, fail closed and
require explicit operator reconciliation; do not blindly append, delete the
journal, or choose a fresh key to bypass it. No automatic override is supplied.

This is a local POSIX filesystem protocol, not a distributed transaction. All
writers to a batch destination must cooperate with its lock. Give it exclusive
ownership while batching: legacy `run_pipeline` append export and external tools
do not acquire this lock and must not write concurrently. Use one authoritative
repository per delivery destination; separate databases do not share delivery
history. The implementation detects observed file drift but cannot serialize a
non-cooperating writer. Atomic replacement avoids partial CSV rows at the target;
a hard process crash can leave an unreferenced temporary file, which is not a
delivered artifact. No unrelated files are cleaned automatically.

## Verification

[Focused tests](../../tests/test_daily_batch.py) cover quota and unit accounting,
decisions, scope/destination/history suppression, representative selection and
ordering, revision identity, blacklist non-enforcement, replay/conflicts, file and
DB failures, interruption recovery, and additive legacy migration. Existing backend
tests continue to exercise the unchanged pipeline and dedupe behavior.
