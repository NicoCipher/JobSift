# JobSift operator service contract V1

JOB-4 · Architecture/service contract · 11 September 2026 · Proposed for review

## 1. Authority and decisions

This is the authoritative proposed service boundary for the future operator interface. The published [operator UI standard](../frontend/operator-ui-standard.md) is binding. This document specifies contracts, not an implemented server, database migration, authentication system, or permission to begin JOB-3. All endpoints below are proposed. “Read V1” means suitable for the first service implementation once its stated evidence prerequisites are met; it does not mean available today.

Decisions:

- One provisioned authenticated operator initially; multiple clients in the model; explicit operator-to-client grants. No public signup, organizations, teams, invitations, subscriptions, seats, or customer self-service.
- REST-style JSON at `/api/v1`, with a same-origin BFF/session boundary in front of the Python service. No GraphQL.
- Python owns domain decisions, evidence projection, access enforcement, and persistence translation. Browser and BFF never read SQLite or recreate matching, dedupe, suppression, freshness, or provenance logic.
- Read-only domain API first. Every domain mutation remains reserved; existing CLI operations are not automatically safe web mutations.
- Capabilities appear both in session client scopes and per-resource responses. They are explicit server decisions, not inferred from failed requests.
- Stable snapshot cursor pagination; 40 rows by default; server-side filtering and sorting. Polling first, SSE later if justified, no WebSockets initially.

### Evidence inspected

- [Domain models](../../job_scout/domain/models.py), [matching](../../job_scout/matching/matcher.py), [pipeline](../../job_scout/orchestration/pipeline.py), [SQLite repository](../../job_scout/storage/sqlite.py).
- [Sourcing plan/report implementation](../../job_scout/sourcing_plan.py), [plan documentation](../sourcing_plans.md), [delivery groups](../delivery_groups.md), [CSV writer](../../job_scout/export/csv_exporter.py).
- [Historical import implementation](../../job_scout/history.py), [historical contract](../historical_operator_state.md), [V1 brief](../../config/search_briefs/taiwo_operator_sourcing_v1.json), [V2 brief](../../config/search_briefs/taiwo_operator_sourcing_v2.json), [V2 baseline](../../validation/taiwo_sourcing_capacity_v2/README.md).

Important limits: `job_matches` is keyed by `(job_id, client_id)` and stores current match evidence, not immutable revision/run history. Sourcing reports contain plan ID and times but no authoritative embedded brief revision or globally stable run ID. `collection_runs` schema existence does not establish populated or client-attributed run evidence. Shared postings/groups have no operator ownership column. Never infer missing relations from filenames, timestamps, matching display names, or directory proximity.

## 2. Boundary and deployment

```text
Untrusted browser / provider text
    │ HTTPS same origin; opaque session cookie; CSRF token on mutations
    ▼
BFF + session boundary (future frontend host)
    │ validates session, origin/CSRF; strips untrusted identity headers
    │ private authenticated service request + short-lived actor assertion
    ▼
Python API / authorization / evidence projection
    │ resolves operator grants, client/resource ownership, capabilities
    ├── controlled sourcing execution policy → existing engine → collectors
    │       (reserved; no bypass or browser-provided network targets)
    └── persistence adapters + registered evidence catalogue
            ├── SQLite today; PostgreSQL adapter later
            └── approved frozen reports/config artifacts; never arbitrary paths
```

Choose BFF option B. A thin same-origin session layer keeps browser secrets out, avoids credentialed cross-origin requests, and provides one place for CSRF/session handling. Python remains the only domain API and authority. A browser-to-Python deployment could work behind a same-origin proxy, but direct cross-origin API calls add CORS/session complexity without an operator-product benefit. No duplicated business endpoints or domain transformation in the BFF.

Public `/api/v1` shapes are preserved through the BFF. The session endpoint is assembled from session facts and Python-authorized scopes. Python validates private service identity plus a signed, audience-bound actor assertion (operator ID, session reference, expiry ≤60 seconds); it resolves grants itself. A service credential alone must not grant all clients or permit actor-header impersonation. Use established signing/transport libraries later; no bespoke cryptography here. Private service exposure and assertion validation are deployment gates.

Development uses the same origin topology through a local proxy. Explicit trusted-development authentication is permitted only with an opt-in development flag, loopback binding, validated Host/Origin, a configured development operator and explicit client grants. It cannot be activated by a request header. Production startup must fail if this mode is enabled; LAN/tunnel exposure requires real authentication. An HTTP loopback cookie may omit Secure only in this mode and must use a separate nonproduction cookie name. CSRF and authorization remain enforced.

Initial production uses HTTPS, one provisioned identity, revocable sessions and private Python service access. Later operators get new operator IDs and explicit grants; they never inherit access by reusing a client's name. Replacing an operator revokes sessions/grants while retaining prior actor IDs in audit history. Storage changes remain behind adapters; no SQL identifiers, database paths, local row offsets, or cursor SQL escape into the browser contract.

## 3. Identity, ownership, and legacy binding

All public identifiers are opaque, case-sensitive strings, URL-encoded as path segments. New service-owned IDs are random UUIDs, persisted once; examples below use readable `example-*` placeholders. Never parse IDs for meaning. Existing domain IDs are retained verbatim. Display names, filenames, revision labels and hashes are attributes, not identity allocation schemes.

| Identifier | Meaning / ownership |
| --- | --- |
| `operator_id` | Authenticated person; provisioned independently of clients. External login subject maps to this stable record. |
| `client_id` | Sourcing subject/history scope. Preserve `taiwo_operator_sourcing_v1` unchanged despite its misleading suffix. Both current briefs share it. |
| `brief_id` | Stable lineage of sourcing criteria for one client, allocated in an explicit registry. |
| `brief_revision_id` | Immutable content revision within that lineage, explicitly registered to exact content/hash; not V1/V2 label or filename. |
| `run_id` | Registered acquisition/evaluation execution or frozen replay report. New random ID plus exact artifact association for legacy evidence; never derive from report filename. |
| `destination_id` | Stable client-bound delivery destination record. Maps server-side to the existing resolved CSV destination string without rewriting delivery history. |
| `posting_id` | Existing `Job.id` / SQLite `jobs.id`; `job_id` is its legacy domain alias, not a group identifier. Public posting objects use `posting_id` only. |
| source identity | Exact tuple `source`, `source_board_id`, `source_job_id`; retain all three. Technical target coordinates are evidence, not client identity. |
| `delivery_group_id` | Existing backend group ID. Group merges can change surviving ID; do not promise eternal aliases that the current store cannot resolve. |
| `history_entry_id` | Stable registry mapping for imported row or delivery audit entry; never expose SQLite row number as globally stable identity. Preserve workbook sheet/row as evidence. |
| `snapshot_id`, `cohort_id` | Service snapshot and registered evidence cohort, respectively. Snapshot is traversal identity; cohort names an evaluation population. Neither creates historical evidence. |

Before exposing legacy data, explicitly register clients, brief lineages/revisions, destinations and approved artifacts in a service catalogue. Persist mappings through storage migration. Registration must verify the artifact's client ID and hash; a changed file cannot silently become the same immutable revision. Historical `created_at`, actor or run association remain null/not reported unless evidenced. Registration time is separate `registered_at`, never substituted for creation/acquisition time.

Authorization is a relation `operator → allowed client → registered resources/evidence`. Global source postings may be shared internally, but a client sees only postings in its registered evaluation/acquisition scope or its own history/delivery evidence. A group detail returns only authorized members and reports `member_completeness`; never leak another client's match, outcome, destination or member count. Aggregate queries apply that same scope before counting. If a legacy report cannot be attributed reliably, do not list it under a guessed client.

Current destination keys include resolved filesystem paths. The registry retains those exact keys privately; moving an export file is not automatically a new delivery destination and must not silently reset suppression. Changing the logical destination is a future explicit operation. No migration/rename is performed in this issue.

## 4. Authentication and capabilities

Use established authentication/session middleware, vendor undecided. Provision the initial operator administratively; no public registration. Login method selection is a deployment decision, not permission to bypass authentication.

Browser session: server-stored revocable session referenced by an opaque random cookie. Production cookie `__Host-jobsift_session`: HttpOnly, Secure, Path=/, no Domain, SameSite=Lax. No session/provider/API credentials in localStorage, sessionStorage, URLs or JavaScript-readable cookies. Rotate session ID on login and privilege change. Server-enforced initial policy: 30-minute inactivity and 12-hour absolute expiry; polling does not extend inactivity. Logout, operator disablement and explicit revocation invalidate sessions immediately, including private-service assertions through the revocation check. These are JobSift defaults to validate during deployment, not externally mandated values. The cookie/session boundary follows [OWASP session guidance](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html).

All browser mutations, including logout and future domain commands, require a session-bound synchronizer token in `X-CSRF-Token` plus exact Origin validation (validated same-origin Referer fallback where appropriate). Reject absent or mismatched origin evidence; SameSite alone is insufficient. Token comes from the no-store session response and stays in memory. Accept JSON for domain mutations, not form-encoded cross-site submissions; GET/HEAD never mutate. This applies [OWASP CSRF guidance](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html). Login/callback CSRF defenses belong to the selected auth protocol and must be reviewed before deployment.

Future automation authenticates separately using revocable service principals and scoped server-held credentials, explicit client grants, expiry and rotation. It does not reuse browser cookies or pretend to be a human. No public automation token issuance endpoint is defined yet. Audit records distinguish human actor and service actor.

Minimum permissions are internal named checks, not a configurable RBAC framework: `read:jobs`, `read:runs`, `read:history`, `read:diagnostics`, `read:briefs`, `write:outcomes`, `write:briefs`, `execute:runs`, `admin:clients`. Client listing is filtered by grants. Every request rechecks session/service identity, client access, resource membership and relevant permission. Query/body client IDs must match the authorized path; supplied IDs never grant access.

### Capability decision

`GET /api/v1/session` returns global and per-client capabilities; resource detail returns narrower `capabilities`. A capability is `{allowed:boolean, reason:null|string}`. Closed reasons: `not_implemented`, `not_authorized`, `evidence_unavailable`, `state_conflict`; allowed requires reason null. Unknown capability keys are treated as unavailable by the UI. Session capability enables a feature for a client in principle; resource capability is decisive for that record, and the server rechecks at execution time. A true session capability never overrides a false resource capability.

Required per-client session keys (resource details include their applicable subset): `can_read_jobs`, `can_read_runs`, `can_read_history`, `can_read_diagnostics`, `can_read_briefs`, `can_edit_outcome`, `can_create_brief_revision`, `can_activate_brief`, `can_start_run`, `can_retry_run`, `can_cancel_run`, `can_view_raw_provenance`, `can_manage_clients`. The first read service enables only implemented authorized reads; all domain mutation keys false with `not_implemented`. Raw provenance is false until redaction/authorization is implemented. Session returns an `authorization_version`; scope changes invalidate caches and snapshots. Capability flags are UI guidance, not security enforcement. Never expose forbidden client IDs in the session.

## 5. Wire conventions, observation and pagination

JSON UTF-8; snake_case properties; plural resource paths; no trailing slash. IDs occupy one encoded segment. GET reads, POST creates/events/commands; no mutations via GET. Successful reads: `200 {data,meta}`; lists put an array in `data` and always include `page`. HTTP timestamps are RFC3339 UTC with `Z`; preserve source timestamp precision. API version, brief schema, sourcing revision, matcher version, and collector version are independent.

Required common `meta`:

- `request_id`: server-generated opaque correlation ID; never trust it as authorization.
- `scope`: `{client_id,brief_revision_id,run_id,destination_id,cohort_id}` with null for not selected/not attributable. A null scope ID must not imply “all clients.”
- `observed_at`: time the underlying evidence was observed, null if unknown. `served_at` is response time. Neither substitutes for the other.
- `data_state`: `available|stale|unavailable`; stale requires a known stale condition/failed refresh and explanation, not arbitrary browser age.
- `completeness`: `complete|partial|unknown` relative to the named evidence scope, not all internet jobs.
- `source_failures`: safe summaries with target/status/evidence reference; empty means no known failures in this response, not proven health when completeness unknown.
- `snapshot_id`: opaque traversal/evidence snapshot ID or null for non-snapshot session/client reads.
- `limitations`: stable machine codes explaining missing attribution, partial coverage or unreported fields.

List completeness and pagination are independent: a 40-row page of a fully known result set is complete in evidence terms. Mixed-time Dashboard sections retain their own metadata; do not manufacture a single common observed_at. Aggregate responses carry their own denominator, unit and evidence reference.

### Availability types

Metric: `{value: number|null, unit: string, availability: reported|not_reported|unknown, definition: string}`. For reported counts value is a nonnegative integer including genuine zero; otherwise value is null. Counts use `postings`, `groups`, `targets`, `requests`, `history_entries`, `clients`, `briefs`, `revisions`, `runs`, `plans`, `aggregate_rows`, or `exports` as explicitly defined. Cost additionally requires currency and actual/estimated basis. `not_reported` means evidence supplies no measurement; `unknown` means evidence explicitly could not determine it. Scalars needing the same distinction use `Fact<T>={value:T|null,availability:...}`. Reported empty list/false is a known negative; null is never converted to zero, false or empty by clients.

A missing optional field means not part of this representation; clients must not infer its value. Required truth-bearing fields use Fact/Metric rather than ad hoc nulls. Timestamps/nullable relation IDs use null plus `limitations` if historically unrecorded. No invented confidence percentages.

### Stable cursor contract

List requests: `limit=40`, permitted 20/40/80; `cursor` opaque; optional `snapshot_id` on initial request to reuse compatible snapshot. First page returns `page={limit,next_cursor,previous_cursor,known_total,snapshot_id,expires_at}`. `known_total` is a reported/not_reported Metric with the list unit. First previous and final next are null. Cursor encodes/binds query, order, limit, principal, authorization version, client, destination and snapshot; sign it or use a server-side random handle. Do not expose SQL or editable offsets.

Read-projection creation is permitted service bookkeeping, not a domain mutation: it must not initialize/migrate a domain database implicitly or use a repository constructor that performs schema changes.

Initial implementation materializes a bounded immutable read projection (ordered IDs AND projected values) for a 30-minute traversal snapshot. Do not hold a SQLite transaction open across requests; IDs alone are insufficient when matches/groups can change. Authorize every page and detail read again. Deny revoked access immediately even to immutable snapshots. Storage can implement snapshots differently later without changing the contract. Snapshot expiration returns 409 `SNAPSHOT_EXPIRED`; UI preserves visible data and offers refresh. Never silently continue against new data. Group merges leave prior snapshot views coherent; outside a snapshot an unresolvable old group is 404, not a guessed replacement.

Changing filter/sort/limit requires a new traversal. Cursor requests may omit bound query parameters; if supplied they must agree or return 400 `INVALID_CURSOR`. Bound `snapshot_id` cannot be moved across client scopes. Detail links carry snapshot_id to keep row/detail evidence consistent. New results require explicit refresh. No infinite-scroll contract.

Server filters use an allowlist; unknown parameters/operators are 422, not ignored. Text `q` is literal case-insensitive search of title/company/location, trimmed, maximum 200 characters; wildcard syntax has no special meaning. Lists use repeated query keys (OR within one key, AND across keys). Dates use `from` inclusive / `before` exclusive UTC. Sort is one key with optional `-` descending; server appends stable public resource ID ascending, null values last. Display sorting is never confined to one page.

## 6. Resource model and evidence rules

### Jobs, groups and postings

Use `/jobs` with explicit `representation=groups` (default) or `postings`. Never mix row types in one result. Response `meta.scope` and each item's `resource_type` establish interpretation. Grouping unavailable yields 409 `REPRESENTATION_UNAVAILABLE` with supported representations, not fabricated singleton groups. UI can explicitly request postings. Detail paths are `/jobs/groups/{delivery_group_id}` and `/jobs/postings/{posting_id}`, avoiding ambiguous “job or group” IDs.

`JobGroupSummary`: resource_type, delivery_group_id, representative_posting (PostingSummary or null), representative_basis Fact, member_count Metric, member_completeness, match (representative MatchEvidence or null), delivery_state, outcome_summary, detail_url. There is no generic `status` and no synthetic group matcher decision. A group filter by decision applies to the authoritative representative's decision. Review may use `representation=postings` to inspect nonrepresentative Needs review evidence; this is explicitly a posting queue, never relabeled groups.

`JobGroupDetail` adds members (PostingSummary array), member completeness/count and a paginated members link when needed, grouping evidence/version when retained, provenance, capabilities. Group details do not silently truncate arrays. Proposed `/jobs/groups/{id}/postings` uses normal pagination for complete member inspection.

`PostingSummary`: resource_type=`posting`, posting_id, source tuple, title, company, location_text, remote_status, first_seen_at, last_seen_at, application_destination and match. `PostingDetail` adds description_text, structured fields, source observation timestamps, grouping relation if known, MatchEvidence, historical/delivery evidence, provenance and capabilities. Raw provider HTML is excluded from normal detail. Job record lifecycle, if exposed, is a separate evidence field, never a service inference from a failed collection.

Representative selection is backend-owned. Prefer a recorded selected posting for the specified run/destination. A server projection may reuse the existing deterministic representative selector only on a verified eligible collected set, recording `representative_basis=selection_in_scope`; it cannot run a global stored-job selection and claim current freshness. If no defensible representative exists, return null with evidence_unavailable and members link, or require posting representation. Read queries never call the collection/export pipeline or mutate suppression state.

`MatchEvidence`: decision exactly `strong_match|possible_match|needs_review|reject`, matched_reasons/rejection_reasons verbatim safe text arrays, matcher_version, evaluated_at, brief_revision_id/run_id (nullable with attribution limitations). `matched_role`, `review_reasons`, `preferred_term_hits` use Fact when structured fields are not stored. Current JobMatch supplies two reason arrays only; do not parse prose heuristically into authoritative structured facts. Versioned server display mappings may format known reason codes later while retaining original evidence. Missing specific review reasons does not erase needs_review. Strong/Possible are eligible categories, not proof of fresh delivery; Needs review and reject are non-delivery-eligible.

**Attribution gate:** legacy matches without immutable brief/run linkage return null relations and `match_revision_unattributed`. A request filtering a specific revision/run must exclude unattributable matches and report coverage, or return HTTP 409 `EVIDENCE_SCOPE_UNAVAILABLE` if no authoritative slice exists. Never relabel current matches with today's active brief. A future immutable match evidence projection keyed by posting/client/revision/evaluation is needed for reliable cross-revision history; this is persistence work, not matcher-semantic change.

### Delivery, history and outcomes

`DeliveryState` separates `destination_id`, `previously_delivered:Fact<boolean>`, `delivery_records`, `historical_suppression:Fact<boolean>`, `historical_evidence_refs`, `fresh_for_delivery:Fact<boolean>`, and `non_delivery_reasons:Fact<array>`. Historical suppression is client-scoped and independent of destination. Prior delivery is client + group + registered destination. No destination selected means prior-delivery/fresh-for-delivery not_reported, not false. Historical identity/URL evidence can still be known.

Only the backend can produce suppression/dedupe/freshness facts using existing policy with explicit observation scope. Stored history alone does not establish a current fresh candidate. A suppression reason names the matching evidence and identity-vs-normalized-URL basis. An absence of export is not by itself a suppression fact.

History entries distinguish `imported_history` and `delivery_event`. Imported entries retain original URL, conservative normalized URL, source tuple if recognized, workbook checksum/import ID, sheet/row, imported_at and operator_status (`applied|not_applied|unknown`). Imported_at is not time applied or first seen. All imported statuses, including unknown and not_applied, represent prior surfacing. Blacklist evidence remains non-enforcing provenance. Delivery events retain posting/group as known, destination and exported_at; they are not application events.

No canonical “latest outcome wins” policy is invented for conflicting imports. `outcome_summary` carries availability plus evidence refs and `resolution=single|conflicting|not_recorded`. Future manual outcome events append actor/time/subject/value rather than overwrite imported rows. Their precedence over imports, group-merge behavior, and reset semantics remain blocked pending explicit policy. Unknown is a known imported status, not an authorization to erase history.

### Briefs and revisions

`Brief` is a stable lineage for a client; revision summaries are children. `/briefs` returns Brief records with `revisions` links and optional active binding Fact. For the requested revision-detail route `/briefs/{brief_revision_id}`, the path parameter is explicitly a revision ID. Creation later uses `/briefs/{brief_id}/revisions`; this distinction is fixed in route docs/OpenAPI and never inferred from identifier spelling.

`BriefRevision`: brief_revision_id, brief_id, client_id, schema_version, revision_label, content_sha256, rules (complete validated SearchBrief fields, excluding duplicated envelope client/schema), created_at, registered_at, provenance, binding Fact, capabilities. Hash is SHA-256 of exact registered artifact bytes for imported files; future revisions store canonical serialized content bytes plus hash algorithm/serialization version. Hash verifies bytes, not identity. Reusing a label or identical content does not silently change IDs. API schema version is independent from source content serialization.

Both current V1 and V2 have `schema_version=operator-style-sourcing-brief-v1`; V1/V2 are sourcing vocabulary revision labels. Keep `client_id=taiwo_operator_sourcing_v1` for both. Activation cannot be inferred from highest revision number. Current plan binding may be reported only from explicitly registered plan-to-artifact evidence. Required/preferred/avoid/ignore, unknown policies, residence versus market/eligibility remain unchanged.

### Runs and metrics

`RunSummary` includes run_id, plan reference, nullable brief revision/destination linkage, status, started/completed, target-count metrics and accounting metrics. Current final status values: `success|partial|failure`. Display mapping may say Failed for failure. Running/Paused/Canceled are reserved future lifecycle states, not produced by this read adapter and not inferred from a pending HTTP response. API V1 clients must treat unknown future state as unrecognized with its evidence, never success; enabling new states requires explicit contract review.

`RunDetail` adds source target outcomes, sanitized failures, retained-results completeness, provenance, optional telemetry and capabilities. Target statuses preserve CollectionStatus values: success, partial, rate_limited, authentication_failure, forbidden, invalid_target, provider_error, network_failure, parse_failure. An orchestration exception may lack trustworthy accounting even if the serialized report defaults counters to zero.

Mappings are exact:

- received/new/changed/unchanged count processed source postings under the recorded pipeline scope; not necessarily unique across overlapping target runs.
- matched counts strong_match + possible_match postings (delivery-eligible decisions before suppression).
- **Legacy rejected counts both reject AND needs_review.** Keep raw metric key `rejected` but definition `legacy_non_delivery_eligible_decisions`; display “Not delivery-eligible,” never “Rejected matches.” A reject-only metric is not_reported unless independently evidenced.
- exported counts CSV rows appended; unit `exports`, definition `csv_rows_appended`. Do not call it fresh groups unless reconciled to actual group selection/delivery evidence. File append and SQLite delivery recording are not atomic today.
- normalized, suppression breakdown, request budget, cost, progress and shortfall are not_reported without evidence. Placeholder zeros from failed target execution are not measured zero; preserve raw report only as provenance and project availability honestly.

Partial/failed engine runs with valid readable reports return HTTP 200; their execution status is data. All-target failure does not mean the service fetch failed. Counts whose population excludes failures carry partial completeness. Stage metrics cannot be added across overlapping accounting windows.

Diagnostics/capacity is explicitly cohort-scoped: 62 is eligible **postings**, 56 is the frozen equivalent V2 fresh **groups** baseline (44 prior equivalent + 12 additional), not actual new exports or daily capacity. V1 title-gate illustrations require their registered frozen evidence before use. Every metric includes definition/denominator; overlapping rejection reasons use reason occurrences and cannot imply a disjoint funnel.

## 7. Endpoint catalogue

All client endpoints use `/api/v1/clients/{client_id}` prefix below. All are read contracts unless marked reserved. Each enforces authorized membership before lookup, including nested IDs and snapshot tokens. Default result order is stable ID ascending unless specified.

| Method/path | Operator question and response | Filters / sort / prerequisites |
| --- | --- | --- |
| GET `/api/v1/session` | Who am I; what may I inspect/do? Session, expiry, client scopes/capabilities, CSRF token. | Auth required; no pagination; no provider secrets. |
| GET `/api/v1/clients` | Whose sourcing can I inspect? Client summaries. | q on display name; display_name then client_id; standard pagination. |
| GET prefix | Which subject/context is selected? Client, briefs/plans links and safe destination summaries. | No implicit default destination/active brief; registry required. Destinations are embedded summaries, not links to an undefined endpoint. |
| GET prefix`/briefs` | Which sourcing lineages exist? Brief summaries. | brief_id; registered_at descending, ID; no guessed creation dates. |
| GET prefix`/briefs/{brief_id}/revisions` | Which revisions belong to this lineage? Revision summaries. | registered_at descending; distinguish chronology from historical creation. |
| GET prefix`/briefs/{brief_revision_id}` | What exact rules are in this revision? BriefRevision. | Optional snapshot_id; registered artifact required. |
| GET prefix`/jobs` | Which leads/postings should I inspect? Homogeneous group or posting summaries. | representation, q, brief_revision_id, run_id, cohort_id, destination_id, decision, source, market, work_mode, outcome, delivery_state, from/before (first_seen). Default decision strong_match + possible_match; explicit needs_review/reject allowed. Default decision priority then first_seen descending then ID. sort=first_seen_at, company, title, decision; reject/unknown last. |
| GET prefix`/jobs/groups/{delivery_group_id}` | Why this lead; which postings support it? JobGroupDetail. | Same evidence scope as list; snapshot_id; scoped representative prerequisite. |
| GET prefix`/jobs/groups/{delivery_group_id}/postings` | What are this group's accessible members? Posting summaries. | snapshot_id, posting_id ordering; no cross-client member leakage. |
| GET prefix`/jobs/postings/{posting_id}` | What is the original posting/evaluation evidence? PostingDetail. | snapshot_id plus evidence scope; no relabeling current match as historical. |
| GET prefix`/runs` | What sourcing occurred? Run summaries. | plan_id, brief_revision_id, source, status, from/before (started_at); started_at descending then ID. |
| GET prefix`/runs/{run_id}` | What succeeded/failed and was retained? RunDetail. | Registered report/client binding; optional snapshot_id. |
| GET prefix`/history` | What was surfaced/delivered/applied before? History entries. | q (title/company/URL), outcome, source, event_type, destination_id, from/before (recorded event time); event time descending, unknown last, ID. Destination filter selects delivery events only; imported history remains available through event_type, never destination-assigned. |
| GET prefix`/history/{history_entry_id}` | What exact record supports this memory? Detailed import/delivery provenance. | Registered stable history identity; permission redaction applies. |
| GET prefix`/diagnostics` | Where did this cohort's results go? Aggregate slices/definitions/drill-down links. | cohort_id or run_id required (mutually exclusive), brief_revision_id optional verified binding; section=pipeline,reasons,sources,suppression,dedupe,capacity (one). Pagination for repeated aggregate rows; pipeline order or count descending then key. |
| GET prefix`/source-health` | What target coverage/failures were observed? Target summaries, times, status, completeness. | run_id or cohort_id required; source/target/status; target identity ascending. No whole-provider health inference. |
| GET prefix`/plans` and `/plans/{plan_id}` | Where is this brief configured to look? Safe plan snapshot, targets, registered brief/destination binding. | Source/brief filter; plan_id ordering; no database/CSV local paths or credential values. |

Snapshot construction must also preserve the authorized detail evidence needed by its rows, or bind to immutable referenced artifacts; a detail request cannot combine old summary values with new mutable evidence.

The two plan reads support brief binding and run inspection; no plan editor is introduced. Group-member/history detail reads enable evidence drill-down. Diagnostic contributing-record links use scoped jobs/history/member endpoints when record IDs are known. If no safe route can represent contributors, `records_url=null` with evidence_unavailable; no speculative universal query endpoint. A requested diagnostic slice that cannot be constructed from the registered evidence returns HTTP 409 `EVIDENCE_SCOPE_UNAVAILABLE`; inability to read its evidence dependency returns HTTP 503 `EVIDENCE_UNAVAILABLE`.

Filtering `delivery_state=fresh|historical|already_delivered|unknown` uses separate backend facts; fresh/already_delivered require destination_id (422 otherwise). Historical never implies already_delivered. Outcome filters use the resolved evidence state; conflicting outcomes are an explicit `conflicting` filter, not arbitrarily assigned. Filtering a recognized field whose required evidence is unavailable returns 409 `EVIDENCE_SCOPE_UNAVAILABLE`, never silently omits the filter. Adjust/remove the evidence-dependent filter or scope, choose a supported representation, or inspect available evidence; retrying the unchanged request is not presumed to help. UI discovers `supported_filters`/`supported_sorts` in list metadata; this is separate from mutation capabilities.

Read evidence missing for a valid resource: return 200 with explicit missing fields if useful data remains. An entire evidence collection that cannot currently be served because its store/report or projection dependency is unavailable returns 503 `EVIDENCE_UNAVAILABLE`, with retry-later recovery and Retry-After when known; a known complete empty collection returns 200 empty array. No invented empty response from a missing artifact.

## 8. Concrete examples

**All ten examples are fictional contract fixtures, not live values or claims about the repository's data.** `example-*` IDs, hashes, cursor handles, session token placeholder and timestamps are illustrative. Required wire envelopes are demonstrated in examples 1–3 and 10. Examples 4–6 and 8–9 show `data` objects/items inside the same mandatory envelope; they do not waive metadata. Example 7 includes the complete partial-state envelope. No placeholder is a real credential.

### 1. Session (response)

```json
{
  "data": {
    "operator_id": "example-operator",
    "display_name": "Example operator",
    "expires_at": "2026-09-11T12:00:00Z",
    "authorization_version": "example-grants-1",
    "csrf_token": "EXAMPLE_ONLY_NOT_A_TOKEN",
    "capabilities": {
      "can_manage_clients": {
        "allowed": false,
        "reason": "not_implemented"
      }
    },
    "client_scopes": [
      {
        "client_id": "example-client",
        "capabilities": {
          "can_read_jobs": {
            "allowed": true,
            "reason": null
          },
          "can_read_runs": {
            "allowed": true,
            "reason": null
          },
          "can_read_history": {
            "allowed": true,
            "reason": null
          },
          "can_read_diagnostics": {
            "allowed": true,
            "reason": null
          },
          "can_read_briefs": {
            "allowed": true,
            "reason": null
          },
          "can_edit_outcome": {
            "allowed": false,
            "reason": "not_implemented"
          },
          "can_create_brief_revision": {
            "allowed": false,
            "reason": "not_implemented"
          },
          "can_activate_brief": {
            "allowed": false,
            "reason": "not_implemented"
          },
          "can_start_run": {
            "allowed": false,
            "reason": "not_implemented"
          },
          "can_retry_run": {
            "allowed": false,
            "reason": "not_implemented"
          },
          "can_cancel_run": {
            "allowed": false,
            "reason": "not_implemented"
          },
          "can_view_raw_provenance": {
            "allowed": false,
            "reason": "not_implemented"
          },
          "can_manage_clients": {
            "allowed": false,
            "reason": "not_implemented"
          }
        }
      }
    ]
  },
  "meta": {
    "request_id": "example-request",
    "scope": {
      "client_id": null,
      "brief_revision_id": null,
      "run_id": null,
      "destination_id": null,
      "cohort_id": null
    },
    "observed_at": "2026-09-11T00:00:00Z",
    "served_at": "2026-09-11T00:00:00Z",
    "data_state": "available",
    "completeness": "complete",
    "source_failures": [],
    "snapshot_id": null,
    "limitations": []
  }
}
```

### 2. Clients list (response)

```json
{
  "data": [
    {
      "client_id": "example-client",
      "display_name": "Example sourcing subject",
      "detail_url": "/api/v1/clients/example-client"
    }
  ],
  "meta": {
    "request_id": "example-request",
    "scope": {
      "client_id": null,
      "brief_revision_id": null,
      "run_id": null,
      "destination_id": null,
      "cohort_id": null
    },
    "observed_at": "2026-09-11T00:00:00Z",
    "served_at": "2026-09-11T00:00:00Z",
    "data_state": "available",
    "completeness": "complete",
    "source_failures": [],
    "snapshot_id": "example-client-snapshot",
    "limitations": []
  },
  "page": {
    "limit": 40,
    "next_cursor": null,
    "previous_cursor": null,
    "known_total": {
      "value": 1,
      "unit": "clients",
      "availability": "reported",
      "definition": "authorized_clients"
    },
    "snapshot_id": "example-client-snapshot",
    "expires_at": "2026-09-11T00:30:00Z"
  }
}
```

### 3. Job group list (request/response)

Request: `GET /api/v1/clients/example-client/jobs?representation=groups&destination_id=example-destination&limit=40`.

```json
{
  "data": [
    {
      "resource_type": "delivery_group",
      "delivery_group_id": "example-group",
      "representative_posting": {
        "posting_id": "example-posting",
        "source": "greenhouse",
        "source_board_id": "example-board",
        "source_job_id": "example-provider-id",
        "title": "Example support role",
        "company": "Example company",
        "location_text": null,
        "remote_status": "unknown",
        "first_seen_at": "2026-09-10T10:00:00Z",
        "last_seen_at": "2026-09-10T10:00:00Z",
        "application_destination": {
          "application_url": null,
          "canonical_url": null,
          "application_url_kind": "unavailable"
        },
        "match": {
          "decision": "possible_match",
          "matched_reasons": [
            "Example stored reason"
          ],
          "rejection_reasons": [],
          "matched_role": {
            "value": null,
            "availability": "not_reported"
          },
          "review_reasons": {
            "value": null,
            "availability": "not_reported"
          },
          "preferred_term_hits": {
            "value": null,
            "availability": "not_reported"
          },
          "matcher_version": "example-matcher",
          "evaluated_at": "2026-09-10T10:01:00Z",
          "brief_revision_id": null,
          "run_id": null
        },
        "resource_type": "posting"
      },
      "representative_basis": {
        "value": "recorded_delivery",
        "availability": "reported"
      },
      "member_count": {
        "value": 1,
        "unit": "postings",
        "availability": "reported",
        "definition": "authorized_group_members"
      },
      "member_completeness": "complete",
      "match": {
        "decision": "possible_match",
        "matched_reasons": [
          "Example stored reason"
        ],
        "rejection_reasons": [],
        "matched_role": {
          "value": null,
          "availability": "not_reported"
        },
        "review_reasons": {
          "value": null,
          "availability": "not_reported"
        },
        "preferred_term_hits": {
          "value": null,
          "availability": "not_reported"
        },
        "matcher_version": "example-matcher",
        "evaluated_at": "2026-09-10T10:01:00Z",
        "brief_revision_id": null,
        "run_id": null
      },
      "delivery_state": {
        "destination_id": "example-destination",
        "previously_delivered": {
          "value": true,
          "availability": "reported"
        },
        "delivery_records": [
          "example-delivery"
        ],
        "historical_suppression": {
          "value": null,
          "availability": "not_reported"
        },
        "historical_evidence_refs": [],
        "fresh_for_delivery": {
          "value": null,
          "availability": "not_reported"
        },
        "non_delivery_reasons": {
          "value": null,
          "availability": "not_reported"
        }
      },
      "outcome_summary": {
        "value": null,
        "availability": "not_reported",
        "resolution": "not_recorded",
        "evidence_refs": []
      },
      "detail_url": "/api/v1/clients/example-client/jobs/groups/example-group?snapshot_id=example-snapshot"
    }
  ],
  "meta": {
    "request_id": "example-request",
    "scope": {
      "client_id": "example-client",
      "brief_revision_id": null,
      "run_id": null,
      "destination_id": "example-destination",
      "cohort_id": null
    },
    "observed_at": "2026-09-10T10:01:00Z",
    "served_at": "2026-09-11T00:00:00Z",
    "data_state": "available",
    "completeness": "unknown",
    "source_failures": [],
    "snapshot_id": "example-snapshot",
    "limitations": [
      "match_revision_unattributed"
    ],
    "supported_filters": [
      "decision",
      "source",
      "q",
      "destination_id"
    ],
    "supported_sorts": [
      "decision",
      "first_seen_at",
      "company",
      "title"
    ]
  },
  "page": {
    "limit": 40,
    "next_cursor": null,
    "previous_cursor": null,
    "known_total": {
      "value": 1,
      "unit": "groups",
      "availability": "reported",
      "definition": "groups_in_registered_scope"
    },
    "snapshot_id": "example-snapshot",
    "expires_at": "2026-09-11T00:30:00Z"
  }
}
```

### 4. Group detail (data object)

The same scoped representative and match evidence are retained; members are PostingSummary objects.

```json
{
  "resource_type": "delivery_group",
  "delivery_group_id": "example-group",
  "representative_posting": {
    "posting_id": "example-posting",
    "source": "greenhouse",
    "source_board_id": "example-board",
    "source_job_id": "example-provider-id",
    "title": "Example support role",
    "company": "Example company",
    "location_text": null,
    "remote_status": "unknown",
    "first_seen_at": "2026-09-10T10:00:00Z",
    "last_seen_at": "2026-09-10T10:00:00Z",
    "application_destination": {
      "application_url": null,
      "canonical_url": null,
      "application_url_kind": "unavailable"
    },
    "match": {
      "decision": "possible_match",
      "matched_reasons": [
        "Example stored reason"
      ],
      "rejection_reasons": [],
      "matched_role": {
        "value": null,
        "availability": "not_reported"
      },
      "review_reasons": {
        "value": null,
        "availability": "not_reported"
      },
      "preferred_term_hits": {
        "value": null,
        "availability": "not_reported"
      },
      "matcher_version": "example-matcher",
      "evaluated_at": "2026-09-10T10:01:00Z",
      "brief_revision_id": null,
      "run_id": null
    },
    "resource_type": "posting"
  },
  "representative_basis": {
    "value": "recorded_delivery",
    "availability": "reported"
  },
  "member_count": {
    "value": 1,
    "unit": "postings",
    "availability": "reported",
    "definition": "authorized_group_members"
  },
  "member_completeness": "complete",
  "match": {
    "decision": "possible_match",
    "matched_reasons": [
      "Example stored reason"
    ],
    "rejection_reasons": [],
    "matched_role": {
      "value": null,
      "availability": "not_reported"
    },
    "review_reasons": {
      "value": null,
      "availability": "not_reported"
    },
    "preferred_term_hits": {
      "value": null,
      "availability": "not_reported"
    },
    "matcher_version": "example-matcher",
    "evaluated_at": "2026-09-10T10:01:00Z",
    "brief_revision_id": null,
    "run_id": null
  },
  "delivery_state": {
    "destination_id": "example-destination",
    "previously_delivered": {
      "value": true,
      "availability": "reported"
    },
    "delivery_records": [
      "example-delivery"
    ],
    "historical_suppression": {
      "value": null,
      "availability": "not_reported"
    },
    "historical_evidence_refs": [],
    "fresh_for_delivery": {
      "value": null,
      "availability": "not_reported"
    },
    "non_delivery_reasons": {
      "value": null,
      "availability": "not_reported"
    }
  },
  "outcome_summary": {
    "value": null,
    "availability": "not_reported",
    "resolution": "not_recorded",
    "evidence_refs": []
  },
  "detail_url": "/api/v1/clients/example-client/jobs/groups/example-group?snapshot_id=example-snapshot",
  "members_url": "/api/v1/clients/example-client/jobs/groups/example-group/postings?snapshot_id=example-snapshot",
  "grouping_evidence": {
    "value": null,
    "availability": "not_reported"
  },
  "provenance": {
    "evidence_ref": "example-delivery",
    "brief_revision_id": null,
    "run_id": null,
    "source_target": {
      "source": "greenhouse",
      "board": "example-board"
    },
    "first_seen_at": "2026-09-10T10:00:00Z",
    "last_seen_at": "2026-09-10T10:00:00Z",
    "raw_payload": {
      "value": null,
      "availability": "not_reported"
    }
  },
  "capabilities": {
    "can_edit_outcome": {
      "allowed": false,
      "reason": "not_implemented"
    },
    "can_view_raw_provenance": {
      "allowed": false,
      "reason": "not_implemented"
    }
  },
  "members": [
    {
      "posting_id": "example-posting",
      "source": "greenhouse",
      "source_board_id": "example-board",
      "source_job_id": "example-provider-id",
      "title": "Example support role",
      "company": "Example company",
      "location_text": null,
      "remote_status": "unknown",
      "first_seen_at": "2026-09-10T10:00:00Z",
      "last_seen_at": "2026-09-10T10:00:00Z",
      "application_destination": {
        "application_url": null,
        "canonical_url": null,
        "application_url_kind": "unavailable"
      },
      "match": {
        "decision": "possible_match",
        "matched_reasons": [
          "Example stored reason"
        ],
        "rejection_reasons": [],
        "matched_role": {
          "value": null,
          "availability": "not_reported"
        },
        "review_reasons": {
          "value": null,
          "availability": "not_reported"
        },
        "preferred_term_hits": {
          "value": null,
          "availability": "not_reported"
        },
        "matcher_version": "example-matcher",
        "evaluated_at": "2026-09-10T10:01:00Z",
        "brief_revision_id": null,
        "run_id": null
      },
      "resource_type": "posting"
    }
  ]
}
```

### 5. SearchBrief revision (data object)

```json
{
  "brief_id": "example-brief",
  "brief_revision_id": "example-revision",
  "client_id": "example-client",
  "schema_version": "operator-style-sourcing-brief-v1",
  "revision_label": "V2",
  "content_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "rules": {
    "target_roles": [
      "Example support role"
    ],
    "target_market": {
      "countries": [
        "United States"
      ],
      "intent": "must",
      "unknown_policy": "review"
    },
    "work_mode": {
      "modes": [
        "remote"
      ],
      "intent": "must",
      "unknown_policy": "review"
    },
    "management_roles": "avoid",
    "excluded_titles": [],
    "excluded_seniority": [],
    "must_have_terms": [],
    "preferred_terms": [],
    "avoid_terms": [],
    "employment_type": {
      "types": [],
      "intent": "ignore",
      "unknown_policy": "review"
    },
    "max_required_experience_years": null,
    "candidate_residence": null,
    "work_eligibility": {
      "countries": [],
      "intent": "ignore",
      "unknown_policy": "review"
    },
    "notes": null
  },
  "created_at": null,
  "registered_at": "2026-09-11T00:00:00Z",
  "provenance": {
    "evidence_ref": "example-brief-artifact",
    "kind": "registered_file",
    "hash_basis": "exact_artifact_bytes"
  },
  "binding": {
    "value": null,
    "availability": "not_reported"
  },
  "capabilities": {
    "can_create_brief_revision": {
      "allowed": false,
      "reason": "not_implemented"
    },
    "can_activate_brief": {
      "allowed": false,
      "reason": "not_implemented"
    }
  }
}
```

### 6. Runs list (data array)

```json
[
  {
    "run_id": "example-run",
    "plan_id": "example-plan",
    "brief_revision_id": null,
    "destination_id": "example-destination",
    "status": "partial",
    "started_at": "2026-09-10T10:00:00Z",
    "completed_at": "2026-09-10T10:05:00Z",
    "metrics": {
      "total_targets": {
        "value": 2,
        "unit": "targets",
        "availability": "reported",
        "definition": "configured_targets"
      },
      "received": {
        "value": 3,
        "unit": "postings",
        "availability": "reported",
        "definition": "received_in_available_target_results"
      },
      "matched": {
        "value": 1,
        "unit": "postings",
        "availability": "reported",
        "definition": "strong_or_possible_decisions"
      },
      "rejected": {
        "value": 2,
        "unit": "postings",
        "availability": "reported",
        "definition": "legacy_non_delivery_eligible_decisions"
      },
      "exported": {
        "value": 0,
        "unit": "exports",
        "availability": "reported",
        "definition": "csv_rows_appended"
      },
      "successful_targets": {
        "value": 1,
        "unit": "targets",
        "availability": "reported",
        "definition": "successful_targets"
      },
      "partial_targets": {
        "value": 0,
        "unit": "targets",
        "availability": "reported",
        "definition": "partial_targets"
      },
      "failed_targets": {
        "value": 1,
        "unit": "targets",
        "availability": "reported",
        "definition": "failed_targets"
      },
      "new": {
        "value": 3,
        "unit": "postings",
        "availability": "reported",
        "definition": "new_postings"
      },
      "changed": {
        "value": 0,
        "unit": "postings",
        "availability": "reported",
        "definition": "changed_postings"
      },
      "unchanged": {
        "value": 0,
        "unit": "postings",
        "availability": "reported",
        "definition": "unchanged_postings"
      }
    },
    "detail_url": "/api/v1/clients/example-client/runs/example-run"
  }
]
```

The corresponding meta is partial, names this run/destination, and includes its failed target. The zero exports is explicitly measured in this fictional fixture, unlike default failure counters.

### 7. Partial run detail (response)

```json
{
  "data": {
    "run_id": "example-run",
    "plan_id": "example-plan",
    "brief_revision_id": null,
    "destination_id": "example-destination",
    "status": "partial",
    "started_at": "2026-09-10T10:00:00Z",
    "completed_at": "2026-09-10T10:05:00Z",
    "metrics": {
      "total_targets": {
        "value": 2,
        "unit": "targets",
        "availability": "reported",
        "definition": "configured_targets"
      },
      "received": {
        "value": 3,
        "unit": "postings",
        "availability": "reported",
        "definition": "received_in_available_target_results"
      },
      "matched": {
        "value": 1,
        "unit": "postings",
        "availability": "reported",
        "definition": "strong_or_possible_decisions"
      },
      "rejected": {
        "value": 2,
        "unit": "postings",
        "availability": "reported",
        "definition": "legacy_non_delivery_eligible_decisions"
      },
      "exported": {
        "value": 0,
        "unit": "exports",
        "availability": "reported",
        "definition": "csv_rows_appended"
      },
      "successful_targets": {
        "value": 1,
        "unit": "targets",
        "availability": "reported",
        "definition": "successful_targets"
      },
      "partial_targets": {
        "value": 0,
        "unit": "targets",
        "availability": "reported",
        "definition": "partial_targets"
      },
      "failed_targets": {
        "value": 1,
        "unit": "targets",
        "availability": "reported",
        "definition": "failed_targets"
      },
      "new": {
        "value": 3,
        "unit": "postings",
        "availability": "reported",
        "definition": "new_postings"
      },
      "changed": {
        "value": 0,
        "unit": "postings",
        "availability": "reported",
        "definition": "changed_postings"
      },
      "unchanged": {
        "value": 0,
        "unit": "postings",
        "availability": "reported",
        "definition": "unchanged_postings"
      }
    },
    "detail_url": "/api/v1/clients/example-client/runs/example-run",
    "targets": [
      {
        "target_identity": "greenhouse:example-board",
        "source": "greenhouse",
        "status": "success",
        "received": {
          "value": 3,
          "unit": "postings",
          "availability": "reported",
          "definition": "target_received"
        },
        "errors": []
      },
      {
        "target_identity": "workday:example-host:example-tenant:example-site",
        "source": "workday",
        "status": "network_failure",
        "received": {
          "value": null,
          "unit": "postings",
          "availability": "not_reported",
          "definition": "target_received"
        },
        "errors": [
          {
            "code": "TARGET_NETWORK_FAILURE",
            "message": "Source target request failed.",
            "evidence_ref": "example-target-failure"
          }
        ]
      }
    ],
    "telemetry": {
      "normalized": {
        "value": null,
        "unit": "postings",
        "availability": "not_reported",
        "definition": "normalized_postings"
      },
      "fresh_deliveries": {
        "value": null,
        "unit": "groups",
        "availability": "not_reported",
        "definition": "reconciled_fresh_group_deliveries"
      },
      "requests": {
        "value": null,
        "unit": "requests",
        "availability": "not_reported",
        "definition": "provider_requests"
      }
    },
    "provenance": {
      "evidence_ref": "example-run-report",
      "brief_revision_id": null,
      "run_id": "example-run"
    },
    "capabilities": {
      "can_start_run": {
        "allowed": false,
        "reason": "not_implemented"
      },
      "can_retry_run": {
        "allowed": false,
        "reason": "not_implemented"
      },
      "can_cancel_run": {
        "allowed": false,
        "reason": "not_implemented"
      }
    }
  },
  "meta": {
    "request_id": "example-request",
    "scope": {
      "client_id": "example-client",
      "brief_revision_id": null,
      "run_id": "example-run",
      "destination_id": "example-destination",
      "cohort_id": null
    },
    "observed_at": "2026-09-10T10:05:00Z",
    "served_at": "2026-09-11T00:00:00Z",
    "data_state": "available",
    "completeness": "partial",
    "source_failures": [
      {
        "target_identity": "workday:example-host:example-tenant:example-site",
        "status": "network_failure",
        "evidence_ref": "example-target-failure"
      }
    ],
    "snapshot_id": "example-run-snapshot",
    "limitations": [
      "brief_revision_unattributed",
      "failed_target_accounting_unreported"
    ]
  }
}
```

### 8. History entry (data item)

```json
{
  "history_entry_id": "example-history",
  "client_id": "example-client",
  "event_type": "imported_history",
  "operator_status": "not_applied",
  "original_url": "https://jobs.example.com/vacancy/example",
  "normalized_url": "https://jobs.example.com/vacancy/example",
  "source": null,
  "source_board_id": null,
  "source_job_id": null,
  "posting_id": null,
  "delivery_group_id": null,
  "destination_id": null,
  "imported_at": "2026-09-10T10:00:00Z",
  "outcome_at": null,
  "provenance": {
    "import_id": "example-import",
    "workbook_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "sheet": "Example sheet",
    "row": 2
  },
  "prior_surfacing": {
    "value": true,
    "availability": "reported"
  }
}
```

### 9. Diagnostics aggregate (data item)

```json
{
  "cohort_id": "example-cohort",
  "brief_revision_id": "example-revision",
  "section": "pipeline",
  "key": "role_title_filter_passes",
  "metric": {
    "value": 2,
    "unit": "postings",
    "availability": "reported",
    "definition": "postings_passing_role_title_filter"
  },
  "denominator": {
    "value": 10,
    "unit": "postings",
    "availability": "reported",
    "definition": "evaluated_postings_in_same_cohort"
  },
  "counting_mode": "distinct_postings",
  "evidence_ref": "example-frozen-report",
  "records_url": null,
  "records_unavailable_reason": "evidence_unavailable"
}
```

### 10. Typed error (HTTP 409)

```json
{
  "error": {
    "code": "SNAPSHOT_EXPIRED",
    "message": "This result snapshot has expired. Refresh to continue.",
    "request_id": "example-request",
    "retryable": false,
    "details": {
      "recovery": "refresh_snapshot"
    }
  }
}
```

## 9. Error contract

Errors use only the envelope in example 10. `details` is code-specific, allowlisted and never raw exception text. Validation errors include `fields:[{path,code,message}]` using JSON Pointer or named query parameter. Messages are safe factual fallback copy; code determines client behavior. A machine error code must map to one semantic recovery class; it must not mean both “change the request” and “retry the service.” Request ID also appears in a response header. No stack traces, paths, cookies, SQL, provider authorization headers or secrets.

| HTTP / code | Meaning |
| --- | --- |
| 401 UNAUTHENTICATED | Missing/expired/revoked identity; prompt login, preserve nonsensitive navigation context. |
| 403 FORBIDDEN | Authenticated and authorized resource scope but insufficient action permission. |
| 404 NOT_FOUND | Missing OR unauthorized client/resource, identical response to prevent enumeration. Check scope before revealing detail. |
| 422 VALIDATION_ERROR | Invalid filter/body/unsupported sort or required destination missing; no silent coercion. |
| 400 INVALID_CURSOR | Malformed/tampered/mismatched traversal parameters. |
| 409 CONFLICT | Domain state conflict or reused idempotency key with different request. |
| 412 STALE_REVISION | If-Match no longer current; return safe current version/reference only if authorized. |
| 428 PRECONDITION_REQUIRED | Missing required concurrency token for a future mutation. |
| 409 SNAPSHOT_EXPIRED / REPRESENTATION_UNAVAILABLE | Requested snapshot/representation cannot be fulfilled; refresh the expired snapshot or choose a supported representation, respectively. |
| 409 EVIDENCE_SCOPE_UNAVAILABLE | Registered evidence cannot satisfy the requested filter, diagnostic slice or evidence scope. Adjust/remove that request constraint or inspect supported evidence; unchanged retry is not presumed to help. |
| 503 EVIDENCE_UNAVAILABLE / SOURCE_UNAVAILABLE | Evidence store/report, projection or source-service dependency is currently unavailable. Retry later when appropriate; Retry-After if known. Changing filters is not the prescribed recovery. This is not an engine run status. |
| 429 RATE_LIMITED | API request budget exceeded; Retry-After seconds; not provider throttling status. |
| 500 INTERNAL_ERROR | Unexpected service failure; correlation only, no leaked internals. |
| 501 CAPABILITY_NOT_IMPLEMENTED | Reserved mutation requested before activation; never a success stub. |

Source-target failure is returned inside a readable run with 200. Partial diagnostics likewise use 200 with partial metadata. A failure to fetch any report is a service/evidence error, not a fabricated failed run. Network timeout after a mutation is an unknown command outcome and uses §10 recovery, never automatic success/failure.

## 10. Reserved mutation and audit contracts

**No domain mutation is supported by the current API: no API exists yet.** All rows below are RESERVED / NOT IMPLEMENTABLE YET until listed domain prerequisites are resolved, audited and capabilities enabled. These reservations constrain later design; they do not authorize implementation or promise behavior the current store supports. No bulk mutations or arbitrary provider URL execution endpoints.

All future commands: authenticated actor, verified client/resource, named permission/capability, browser CSRF, required Idempotency-Key, required If-Match where a resource exists, and transactionally recorded operation/audit event. Server supplies actor/time; reject caller-supplied actor, client mismatch, or before-state. Result is authoritative resource plus operation reference, never optimistic fabricated UI state.

| Reserved route | Permission/capability | Body / concurrency / result / blocker |
| --- | --- | --- |
| POST prefix`/outcome-events` | write:outcomes / can_edit_outcome | Subject `{type:posting\|history_entry,id}`, value `applied\|not_applied`; If-Match on current outcome projection, including empty initial projection. Append event with actor/time; 201 event + resulting projection. Reset/group subjects reserved until precedence, conflict and merge/reset policy is approved. Imported rows never overwritten. |
| POST prefix`/briefs/{brief_id}/revisions` | write:briefs / can_create_brief_revision | Full validated rules + base_revision_id; If-Match on lineage head. 201 immutable BriefRevision; stale head 412. Requires revision registry and immutable content/audit storage. No implicit activation. |
| POST prefix`/plans/{plan_id}/brief-binding-events` | write:briefs / can_activate_brief | brief_revision_id within same client; If-Match on plan binding. 201 event + binding; explicit plan scope rather than ambiguous global active brief. Requires binding storage and concurrent-run snapshot policy. |
| POST prefix`/runs` | execute:runs / can_start_run | Registered plan_id, exact brief_revision_id and destination_id; If-Match on approved plan version. 202 operation and run reference only once durable execution identity exists. Requires durable single-writer execution, budgets, audit and CSV/database reconciliation policy. |
| POST prefix`/runs/{run_id}/retry-requests` | execute:runs / can_retry_run | Explicit authorized failed target IDs; If-Match on run; creates a NEW linked run, never rewrites old evidence. 202 operation/new run. Retry acquisition versus delivery semantics and crash recovery must be settled first. |
| POST prefix`/runs/{run_id}/cancel-requests` | execute:runs / can_cancel_run | If-Match on lifecycle; 202 cancellation request, not Canceled run. Requires cancellation checkpoints, retained-results and terminal-state race policy. Completed run conflict 409; no retroactive rollback. |
| Future review disposition | No route/capability enabled | Relevance vocabulary, effect, audit subject and precedence undefined. Do not accept relevant/not_relevant as outcomes or mutate matcher output. |

No pause endpoint is reserved merely to fill the UI vocabulary. Client administration also remains provisioning-only; can_manage_clients=false until its own ownership/migration contract exists.

Idempotency: key opaque, 16–128 characters; scoped to principal + client + route/action. Persist canonical request digest, operation state and terminal response; same key/body returns same operation/result, different body 409. Check authentication/authorization and existing key before reapplying concurrency tests to a replay. Retain full replay response at least seven days and a durable key/digest tombstone thereafter; expired result returns conflict/recovery reference, never reexecutes a command. This avoids duplicate action after delayed retry.

Future editable resource reads must supply the applicable opaque version token: outcome projection in posting/history detail, lineage version in Brief summaries, plan binding/version in plan detail, and lifecycle version in run detail. If-Match references that token, not a collection ETag.

For future commands reserve GET prefix`/operations/{operation_id}` and GET prefix`/operations?key={idempotency_key}` (key lookup scoped to actor/client, no secrets in keys). They are not read-V1 endpoints until mutations exist. After timeout, query operation by key or repeat the identical command/key; never invent a new key to retry uncertain work. If no authoritative resolution exists, show pending/unknown and require reconciliation. Async acceptance is not completion. Failure records per-target/per-effect results; domain audit failures prevent mutation success.

Concurrency: opaque strong ETag from current resource version; If-Match required on listed mutable projections. New collection POSTs use the parent resource ETag. Commands append immutable events; audit includes actor type/ID, client, resource/action, server timestamp, before/after version or event payload, request ID, idempotency identity, operation ID and resulting resource. Store audit atomically with domain state/command acceptance. No complete audit infrastructure is implemented here.

## 11. URL, provenance and untrusted content policy

`ApplicationDestination={application_url,canonical_url,application_url_kind}`. Kind `direct_apply` only for a trustworthy provider-supplied vacancy-specific apply_url; otherwise exact safe canonical vacancy URL with `vacancy_page`; otherwise null application_url and `unavailable`. Never append `/apply`, reconstruct a host/path, return a careers homepage as a vacancy, or normalize away meaningful identity parameters in the browser. Canonical_url can remain a safe known listing even if direct application is unavailable. Untrusted/unsafe values remain non-clickable redacted evidence if needed, not actionable destinations.

Service validates HTTP(S) schemes, no embedded credentials, no localhost, private-network or link-local destination, and vacancy-specific provenance; HTTPS preferred, known original public HTTP may remain exact if explicitly allowed. Do not infer trust from text saying “apply.” Redirect following/prefetch by the server is not needed for read API; if ever introduced, each redirect/DNS resolution needs SSRF defenses. External link navigation uses noopener/noreferrer and an explicit destination label. No server fetching of arbitrary URLs supplied by the browser.

Summary responses exclude raw payloads and HTML. Detail exposes plain description_text, safe source tuple/target coordinates, first/last seen, source published/updated timestamps separately, registered brief/run references, matched reasons, history/delivery refs and destination. Original provider text remains untrusted even after JSON encoding. Frontend output-encodes text; any future rich HTML rendering requires a maintained allowlist sanitizer, no scripts/handlers/iframes or unsafe links, plus CSP. Malicious instructions in job descriptions are data, never executable commands or authorization.

Raw provenance, when eventually enabled, requires can_view_raw_provenance on the resource, field-level redaction, bounded size and audit access. No default raw-payload route now. Evidence references resolve only through allowlisted registered resources, not arbitrary filesystem/download paths. Normal operators must still see safe match reasons without raw access. Sensitive local paths, provider credentials and other-client metadata never pass through logs or errors.

## 12. Security and execution controls

| Threat | Enforceable boundary |
| --- | --- |
| IDOR / forged client or nested ID | Python verifies grants plus resource/client membership on every read, count, cursor and command. Same 404 for absent/inaccessible scope. |
| Forged operator headers | Public edge strips identity headers; Python accepts only authenticated BFF assertion, rechecks session revocation and grants. |
| CSRF | Same-origin JSON mutations, session-bound token and validated origin; GET has no effects. |
| XSS / raw HTML / malicious job content | Plain text default, output encoding, safe URL validation, sanitizer only if rich text later justified; restrictive CSP at frontend deployment (self scripts, no unsafe-eval, no unapproved framing). |
| SQL/search injection | Parameterized values and mapped allowlisted sort/filter identifiers; no raw SQL parameters or arbitrary JSON query language. |
| Unrestricted runs / excessive provider requests | Execution service accepts registered plan/target IDs only, reuses collector controls, serializes writes per database/destination, enforces authorized budget policy. |
| Secrets/log exposure | Server-held credential references, response allowlists, redacted structured errors/logs; no authorization headers/tokens in diagnostic details. |
| Cache/provenance cross-client leak | Client/principal/grants/evidence scope in keys; authorize before serving cached evidence; safe redacted nested refs. |
| Resource exhaustion | limit ≤80; bounded q/body/response sizes, request timeouts, snapshot quotas, read budgets and eventual execution concurrency limits. |

API rate limits are distinct from external-provider request controls. Initial service defaults: 120 authenticated reads/minute/operator with burst 20, at most 10 concurrent read requests/operator and 5 active traversal snapshots/operator; configurable server policy, 429 + Retry-After on excess. Authentication attempts have separate bounded throttling chosen with auth integration. Snapshot quota exhaustion returns 429 rather than discarding a user's active snapshot silently. Shared-instance limits also apply; clients cannot choose unlimited limits.

Future execute:runs requires independent controlled execution admission. V1 collector retries, Workday request caps and existing Greenhouse/Ashby/Lever safety remain authoritative. Browser cannot submit arbitrary request budgets, credentials, host targets or disable flags. Scheduling/automation uses the same policy. API request allowance never implies permission to make that many provider calls. No provider requests on ordinary GET pages, refresh, or capability discovery.

Credentials stay on server; config uses secret IDs/names, not returned values. No secrets infrastructure is added. Errors/logs redact tokens, cookie values, provider headers and sensitive paths; audit IDs remain usable for diagnosis. Logs of free provider error text require sanitization before recording or presentation.

## 13. Caching, versions and updates

Resource ETags used for future concurrency are returned even when HTTP caching is disabled. No public/shared HTTP caching. Session/auth/client-scope responses: `Cache-Control: no-store`. Other authorized read endpoints initially also no-store at the browser boundary; ETag-based server-side private revalidation may be added without relaxing authorization. Internal snapshot/evidence caching is permitted and distinct from browser caching. Key includes client, destination, principal/grant version, filters/order, revision/run/cohort, representation and redaction policy. Frozen artifacts may remain internally cached by hash for 24 hours; authorization is always fresh. No distributed cache required.

Future mutation success invalidates affected jobs/history/outcomes, brief bindings, run/health/diagnostic views and capability projections. Existing immutable snapshots remain explicitly historical until expiry; UI starts a new snapshot to reflect changes. Authorization changes revoke snapshots immediately. BFF does not serve stale authorized payloads after logout or client revocation.

`/api/v1` is the public major version. Breaking semantics/required fields/removals require `/api/v2` with coexistence/migration documentation. Optional additions are backward compatible only when they do not change existing meaning. Clients ignore unknown object fields; never map unknown enum values to a known business state. Brief revision numbers, matcher releases and collector releases are evidence attributes, not API version changes.

Use [OpenAPI 3.1](https://spec.openapis.org/oas/v3.1.1.html) as the executable wire contract when implementation starts, generated/validated against serializers and contract fixtures. Model discriminated representations, Fact/Metric availability, status enums, capabilities and errors explicitly. This issue deliberately creates one Markdown authority rather than a speculative schema file.

Live updates: fetch on navigation/manual refresh first. When durable authoritative running state exists, poll run detail every 5 seconds while visible/active, back off to 15 then 30 seconds on transient errors, honor Retry-After, stop on terminal state and hidden tab. Polling must not refresh session inactivity. Counts remain “so far” and unreported progress stays unreported. SSE is the next option for server-to-browser progress if polling costs/latency justify it, through the same auth BFF with reconnect/snapshot reconciliation. WebSockets require demonstrated bidirectional real-time needs; none exist here.

## 14. UI authority coverage and acceptance review

| UI surface | Authoritative resources | Unsupported / reserved |
| --- | --- | --- |
| Dashboard | Session/client scope; scoped jobs counts; runs; source-health; diagnostics capacity cohort. Keep each section's observed_at/completeness. | No invented combined health score, live baseline, or global attention total without compatible scope. |
| Jobs | Group/posting lists/details, member details, destination-scoped delivery and history evidence. | Fresh filtering without evidenced collection/delivery scope; manual outcomes. |
| Review | Posting representation filtered needs_review/possible_match/strong_match; detail MatchEvidence and provenance. | Persisted reviewed/relevant labels, inferred reason categories, automatic outcome on URL opening. |
| Search Briefs | Briefs/revisions, plan binding, registered hashes/rules and comparison from exact revisions. | Revision creation/activation until persistence/audit gates pass. |
| Runs | Registered run list/detail and target outcomes; plans. | Start/retry/cancel/pause, live states/progress until durable execution contract. |
| History | History list/detail, scoped group deliveries, import provenance. | Rewriting imported statuses, guessed application timestamps, unknown-as-not-applied. |
| Diagnostics | Scoped aggregate metrics and contributing-record links; source health; groups/members. | Unrecorded reject-only counts, unknown denominator percentages, fabricated drill-down records. |
| Settings | Session/operator/client context and capabilities. Nonsecret theme/density/shortcut/timezone preferences may remain browser-local. | Client management, provider credential editing, user/team administration, server preference writes. |

Acceptance requirements for later implementation:

1. A UI can perform all supported reads without filesystem/SQLite access. BFF calls only service contracts; browser has no engine implementation.
2. Contract fixtures stay unchanged when SQLite adapters are replaced with PostgreSQL adapters. Persisted public IDs/catalogue mappings survive migration.
3. Two clients with overlapping postings, separate histories and destinations cannot see each other's evidence/counts/cursors; forged path/body/query/nested IDs fail server-side.
4. Adding/replacing an operator changes grants, not client/brief IDs or historical actors. Revocation invalidates both sessions and cached snapshot access.
5. Null/not_reported, explicit unknown, measured zero, partial coverage and service failure have distinct tested outputs. Failed target default zeros are not presented as observations.
6. Every displayed decision/suppression/group representative/aggregate links to registered backend evidence, or explicitly reports unavailable attribution. No revision/run guessing from current state.
7. Both briefs retain the legacy client ID and V1 schema discriminator. V2 baseline 56 groups and 62 eligible postings keep their distinct units and frozen scope.
8. Exact four matcher decisions remain separate from outcomes; legacy rejected counter includes Needs review and is labeled accordingly.
9. Cursor replay under concurrent writes preserves order AND values; expiry/mismatched scope produces typed error, not mixed pages. Counts and drill-down share scope.
10. Unauthorized/missing resources do not disclose existence; HTML/URL injection, SQL filter injection, forged actor and browser CSRF tests fail safely. Secrets never appear in payload/error/log fixtures.
11. Unsupported capabilities are false with reasons; reserved commands cannot produce success stubs. If activated later, duplicate/timeout/conflict tests demonstrate authoritative recovery and audit.
12. Valid partial/failed run returns 200 data; an unavailable evidence service/report yields 503 `EVIDENCE_UNAVAILABLE`, while an unsatisfiable evidence-dependent request yields 409 `EVIDENCE_SCOPE_UNAVAILABLE`. Verify distinct retry-service versus adjust-request recovery. Ordinary reads cannot trigger collector requests or delivery writes.

Review conclusions: the boundary permits the first operator UI, later clients/operators and storage migration without conflating identity. Missing data is first-class, and capability/evidence gates keep unsupported controls absent. The design deliberately avoids organization/RBAC/billing infrastructure. It is enforceable on the server, provided the prerequisites below are implemented before exposure; this document does not claim a deployed secure service.

### Remaining implementation gates

No unresolved architectural choice blocks this documentation. Actual deployment requires: chosen login/session integration and private assertion validation; stable catalogue/grant registration; trustworthy legacy resource/client bindings; snapshot projection and scope enforcement. Richer historical revision/run reads require immutable match attribution. Group representative/freshness and detailed suppression reads require evidence-preserving adapters to existing policy, not new matching rules. All mutations remain blocked by their specific event/concurrency/execution/reconciliation prerequisites. These gaps must be reported as unavailable/reserved until resolved, not filled by the frontend.
