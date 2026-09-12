# Operator Outcomes V1

JOB-7 supplies backend outcome policy and additive persistence for the reserved
[outcome command](../api/operator-service-contract-v1.md). It does not implement
HTTP, authentication, browser writes, or frontend capabilities. The explicit policy
here resolves the outcome-precedence prerequisite for an individual subject;
conflicting historical entries are not merged or assigned an arbitrary winner.

## Separate facts

Matcher decisions (`strong_match`, `possible_match`, `needs_review`, `reject`),
delivery/surfacing evidence, and operator outcomes are independent dimensions.
Applied does not imply relevance; Not Applied does not imply rejection; Unknown
does not imply freshness. Opening a URL does not write an outcome.

Writable outcomes are exactly `applied` and `not_applied`. `unknown` is a read or
import-evidence value, never a reset command. Reset, group outcomes, review
labels, relevance labels, and cross-subject transfer remain unsupported.
Historical blacklist evidence is not consulted.

## Domain and store

- [Domain models](../../job_scout/domain/operator_state.py):
  `OperatorOutcomeSubject`, `OperatorOutcomeCommand`, `OperatorOutcomeEvent`,
  `OperatorOutcomeProjection`, `OperatorOutcomeResult`, `SurfacingEvidence`,
  `OperatorOutcomeImportRecord`, `OperatorOutcomeImportReceipt`, `OutcomeConflict`.
- [Store](../../job_scout/storage/operator_state.py): `OperatorStateStore(repository)`.
- [Tests](../../tests/test_operator_state.py).

The store exposes `record_outcome(command)`, `get_projection(client_id, subject)`,
`list_events(client_id, subject)`, `get_surfacing_evidence(client_id, subject)`,
`import_outcome(record)`, and `get_import_receipt(...)`. Reads return immutable
models/tuples; event lists are ordered by version. Replay results are historical
snapshots; use `get_projection` for the current projection.

Commands require client, subject, writable value, expected integer version,
actor type/ID, opaque 16–128 character idempotency key, and source reference.
Unknown fields, including caller `recorded_at`, are rejected. The store generates
UTC recording timestamps. Actor identity and source references are supplied by a
trusted server-side caller; this is not authentication or proof of an external
file's contents. Future API/auth code must authenticate the actor, authorize the
client/subject, and prevent browsers from supplying trusted actor fields.

## Subjects and surfacing eligibility

`posting` uses the persisted jobs ID. An existing unsurfaced posting projects
Unknown/version 0, but cannot receive an explicit outcome. Writes require the
**exact posting ID** in `exports` or as recorded representative `job_id` in
`group_deliveries`, for that client. A member of the same current practical group
is not sufficient. Destination is preserved on surfacing evidence; the outcome
itself is client + exact subject scoped, independent of destination.

`history_entry` uses the positive decimal `historical_job_links.id` within this
repository. The row must exist and belong to the client. It needs no synthetic
Job row. This local domain identifier must be mapped by the future service's
opaque history-resource registry, not exposed as a globally stable SQLite ID.

The store reads existing facts, without creating a second surfaced flag or table.
Historical status never grants a matching current posting write eligibility.
Outcome events remain attached to their original posting after practical-group
merges; there is no propagation to other members or between history and postings.
These domain checks are not a general resource-read authorization layer.

## Projection precedence

For a history entry:

1. Latest explicit event for that exact client/subject.
2. That row's imported `operator_status`.
3. Unknown where no explicit state exists.

For a posting: latest explicit event, otherwise Unknown. Imported history for a
similar or identical vacancy is not automatically transferred to the posting.

A projection reports current value/source, baseline value/source/import ID,
version, and latest event ID. Initial version is 0. An explicit event increments
version even if the value repeats; it records a new explicit observation. Ordering
uses accepted event versions, not caller timestamps or UUID ordering.

A historical Not Applied baseline followed by an Applied event projects Applied
while retaining Not Applied and its import provenance. The store never updates
`historical_job_links.operator_status`. Separate contradictory historical rows
remain separate subjects; ambiguous import resolution fails closed.

## Events, concurrency, and replay

Events retain event ID, client/subject, value, version, previous value/version,
actor type/ID, server timestamp, action, idempotency identity, canonical request
SHA-256, and source reference. The original event/result projection is persisted.

A write transaction uses `BEGIN IMMEDIATE`, reads the current version, requires
`expected_version` to match, and appends exactly one event. Stale versions raise
`OutcomeConflict`; there is no silent last-write-wins behavior. Surfacing is
rechecked in the same transaction.

Command keys are scoped by actor type + actor ID + client + `record_outcome`
action. Identical canonical requests return the original event/result, even after
later events advance the projection. Replay lookup precedes the concurrency check.
Incompatible reuse conflicts. Replay results are retained indefinitely; no expiry
or reexecution boundary is introduced. Future authorization must be checked before
calling this trusted store, including for replays.

## Ongoing imports

The minimal adapter boundary accepts one `OperatorOutcomeImportRecord`. No workbook
parser or CLI is added. An adapter supplies a stable external observation ID,
source/import reference, actor, client, explicit subject type, identity evidence,
observed outcome, and expected projection version. A changed observation requires
a new external ID and an appropriate version; it does not overwrite the old receipt.

Resolve within the chosen subject type, in this order:

1. Exact source + board + provider job ID (all three required together).
2. If no identity match exists, exact normalized vacancy URL.

A recognized provider URL can supply its tuple using existing `source_identity`.
URLs use the existing `canonicalize_url` helper; stored canonical/normalized URL
columns are compared exactly. Identity ambiguity is terminal, even if URL fallback
could choose one candidate. Zero or multiple matches fail closed. No title,
company, fuzzy match, practical-group expansion, or cross-subject fallback occurs.
Resolution alone does not authorize a posting: exact delivery evidence is still
required. Historical-only entries resolve against client-scoped imported rows.

Applied/Not Applied imports append explicit events under the same concurrency and
surfacing rules. Their accepted versions determine projection precedence; an
external timestamp does not silently reorder state. Unknown imports append an
immutable receipt only, leave projection/version unchanged, and never reset an
explicit event or imported historical baseline. They also require the expected
version and valid surfaced subject. `get_import_receipt` exposes original input,
server receipt timestamp, and the result snapshot, including unknown observations.

Import replay is scoped by actor type/ID + client + source reference + external
record ID, under the separate `import_outcome` action. The canonical input digest
is checked before re-resolution or version validation. Identical retries return
the original result even if current identity/state has changed; incompatible
reuse conflicts. Receipt and any event commit in one transaction.

## Additive storage and Daily Batch

Opening the store creates only `operator_outcome_events`,
`operator_outcome_imports`, and their constraints/triggers. Existing jobs, matches,
delivery/group records, historical imports/blacklists, and Daily Batch tables are
not rewritten. Event version and actor/client/action/key uniqueness are enforced
in SQLite. New event/import tables reject UPDATE and DELETE through triggers;
polymorphic subject existence and client membership are checked transactionally.
No group foreign key makes event history depend on mutable group identity.

[Daily Batch V1](../batching/daily-batch-v1.md) already records delivered
representatives through exports/group deliveries. Finalizing a batch therefore
makes its exact representatives writable without adding a new surfacing event or
changing batch counts, selection, or finalization. Outcome writes never alter
matcher rows or delivery records and cannot make a delivered group fresh again.

## Reserved integration

The future `POST /api/v1/clients/{client_id}/outcome-events` layer can map subject,
value, and version to these commands. It still must implement authentication,
authorization, CSRF, opaque resource IDs/If-Match tokens, Idempotency-Key handling,
and operation/audit responses. No HTTP status mapping or mutation capability is
enabled by this domain implementation. JOB-31 remains read-only. Group/reset and
cross-subject conflict/merge semantics remain reserved.
