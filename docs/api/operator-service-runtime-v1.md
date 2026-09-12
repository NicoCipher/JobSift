# Operator service V1 runtime

JOB-32 implements the read foundation beneath the binding
[service contract](operator-service-contract-v1.md). It does not connect the
frontend fixture transport or implement the BFF, production authentication, or
any mutation. No core matcher, dedupe, Daily Batch, or outcome logic was changed.

## Package and optional dependencies

`job_scout/service/` contains:

- `app.py`: FastAPI factory, HTTP envelopes, route handlers and response orchestration.
- `config.py`, `catalog.py`: explicit private configuration and validated registrations.
- `auth.py`: trusted-development Host/Origin checks and read admission.
- `read_store.py`: bounded read-only SQLite adapter and evidence presentation.
- `schemas.py`, `errors.py`: Pydantic wire models and one typed error contract.
- `queries.py`: allowlisted filters/sorts and generated query parameter documentation.
- `snapshots.py`: immutable process-local values, random handles and traversal quotas.
- `__main__.py`: loopback launcher; `__init__.py` has no server side effects.

Optional `service` dependencies are FastAPI **0.141.1** (ASGI routes, validation,
serialization and OpenAPI) and Uvicorn **0.52.4** (server). Core CLI dependencies
remain unchanged. The implementation environment installed them with:

```sh
.venv/bin/python -m pip install 'fastapi==0.141.1' 'uvicorn==0.52.4'
```

For a fresh local environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,service]'
JOBSIFT_SERVICE_CONFIG=config/operator.service.local.json .venv/bin/python -m job_scout.service
```

The launcher takes its loopback host and port from validated configuration and
disables Uvicorn proxy-header trust. Do not expose this mode through a LAN,
tunnel or public reverse proxy. GET `/healthz` means process liveness only.
GET `/openapi.json` supplies OpenAPI 3.1 generated from the actual response models,
routes and query grammar. Interactive Swagger/Redoc pages are not enabled.

## Explicit private catalogue

The JSON file path is mandatory through `JOBSIFT_SERVICE_CONFIG`; missing or
invalid configuration fails startup. `*.service.local.json` is ignored by Git.
Paths below are fictional relative examples, resolved against the config file:

```json
{
  "mode": "development",
  "trusted_development": true,
  "bind_host": "127.0.0.1",
  "origin": "http://127.0.0.1:8000",
  "operator_id": "example-operator",
  "operator_display_name": "Development operator",
  "allowed_client_ids": ["example-client"],
  "database_path": "example-domain.db",
  "clients": [{"client_id": "example-client", "display_name": "Example client"}],
  "destinations": [],
  "briefs": [],
  "history": [],
  "evidence": []
}
```

Provision the existing domain DB outside the service. No GET creates it.
Register clients explicitly; the service never enumerates clients from database
rows, directories, or filenames. Grants reference those registrations and cannot
contain a wildcard. Preserve `taiwo_operator_sourcing_v1` when registering Taiwo.

A destination registration contains `destination_id`, `client_id`, `display_name`
and the exact private `domain_key` already used by exports/group deliveries.
A brief registration contains `brief_id`, `brief_revision_id`, `client_id`,
`revision_label`, `artifact_path`, exact-byte `content_sha256`, and timezone-aware
`registered_at`. Hash and embedded client ID are validated at startup. Rules are
validated by the existing SearchBrief model and retained as immutable registered
content for this process. Restart/re-provision to adopt another registered artifact;
editing a file never changes a running revision's cached rules.

V1/V2 labels are vocabulary revisions, not schema versions. Both use
`operator-style-sourcing-brief-v1` and may share the same client and lineage.
Creation time and active binding are unavailable; neither filename nor newest
registration activates a revision.

Public history identities must also be provisioned explicitly and retained across
restarts/storage migration. Allocate opaque IDs once during provisioning (random
UUIDs are appropriate). Each `history` registration contains `history_entry_id`
and `client_id`, plus either `historical_row_id` for an existing imported row, or
`posting_id` + `destination_id` for a recorded delivery. Local row numbers and
private destination strings never appear as public identity fields. A missing
mapping in a client's history collection returns `409 EVIDENCE_SCOPE_UNAVAILABLE`;
it does not silently omit rows or invent IDs during GET.

Optional `evidence` registrations accept `evidence_id`, `client_id`, `kind`
(`run`/`diagnostics`), `artifact_path` and `content_sha256`. Their hashes are checked,
but these registrations do not yet enable Run/Diagnostics serializers or routes.
The catalogue is persisted in the private configuration file, outside domain
SQLite. It contains provisioning data, not an HTTP administration surface.

## Trusted development identity

Development identity is disabled by default. Startup requires development mode,
explicit opt-in, configured operator and client grants, and loopback binding/origin.
Production mode fails startup because production authentication is not implemented.
Host must equal the configured origin authority; a supplied browser Origin must
match exactly. Non-loopback ASGI server addresses are rejected. Public identity
headers never supply operator identity or grants. No session cookie or fictional
production CSRF token is minted; the response labels trusted development explicitly.

Every nested client route checks its grant before reading evidence or snapshots.
Absent/inaccessible clients use the same typed 404. The stable authorization version
is derived from the validated configuration; changed configuration requires a
restart and invalidates process-local traversals. Production session revocation,
BFF assertion verification, CSRF and login remain deployment gates.

Session and resource capabilities enable only Jobs, History and Brief reads.
All mutation/execution/management capabilities, Runs/Diagnostics reads and raw
provenance remain false with `not_implemented`. Domain outcome persistence does
not enable an HTTP outcome write.

## Implemented endpoints

All routes below use GET only:

- `/api/v1/session`
- `/api/v1/clients` and `/api/v1/clients/{client_id}`
- `/api/v1/clients/{client_id}/jobs`
- `/api/v1/clients/{client_id}/jobs/postings/{posting_id}`
- `/api/v1/clients/{client_id}/jobs/groups/{delivery_group_id}`
- `/api/v1/clients/{client_id}/jobs/groups/{delivery_group_id}/postings`
- `/api/v1/clients/{client_id}/history` and `/history/{history_entry_id}`
- `/api/v1/clients/{client_id}/briefs`
- `/api/v1/clients/{client_id}/briefs/{brief_id}/revisions`
- `/api/v1/clients/{client_id}/briefs/{brief_revision_id}`

All lists include `data`, `meta`, and `page`, with default limit 40 and allowed
20/40/80. Client lists use the contract's standard traversal; session/client detail
are non-snapshot reads. Jobs allow `q`, repeated `decision`, repeated `source`,
`destination_id`, representation and approved sorts. Default decisions are Strong
and Possible; default ordering is decision priority, first_seen descending, ID.
Text search is literal and case-insensitive, not SQL wildcard syntax. Explicit
sorts use null-last and an ascending resource-ID tie-break. Unsupported parameters
are 422; recognized Jobs evidence filters that cannot be attributed are 409 scope
errors. No current match is relabeled with a registered V2 revision.

History supports text, source, outcome, event type and destination filtering;
destination filtering selects delivery events. History is newest recorded event
first. Brief lineages/revisions use registration ordering, never guessed creation
or activation time. Member lists use posting ID order. Runs, Diagnostics, source
health, Plans and all reserved mutations are unimplemented; absent routes return
typed NOT_FOUND. There is no execution or mutation success stub.

## Read-only database and projection limits

The adapter opens `Path.as_uri() + '?mode=ro'`, with `uri=True` and
`PRAGMA query_only=ON`, then a short read transaction. It never constructs
SQLiteRepository, DailyBatchStore, or OperatorStateStore. Schema initialization,
backfill, snapshot writes, outcome writes and delivery updates cannot occur.
A missing store/table needed by a read returns typed 503, without SQL/path text.
Missing optional outcome-event tables yield the legacy baseline/Unknown projection;
missing posting-group mappings permit postings and make groups unavailable.

Reads are bounded to 10,000 rows per evidence query by default, configurable up
to 100,000; serialized snapshot budget defaults to 16 MB, capped at 64 MB. SQLite
value size is bounded and its progress handler interrupts queries after five
seconds. Oversized projections fail with a scope error instead of truncating
truth-bearing arrays. SQLite dependency errors return a service error.

Posting visibility requires a client match or exact client delivery record. Shared
group membership alone grants nothing. Group members and their count include only
authorized postings; `member_completeness=unknown` avoids revealing hidden totals.
Groups use persisted IDs, never new title/fuzzy grouping.

A defensible representative is a recorded client/destination group-delivery
representative. Without that evidence, group detail has null representative and
unreported basis; group-list filtering requiring an unavailable representative
returns 409 `REPRESENTATION_UNAVAILABLE`, with postings as the recovery. The adapter
does not select a representative from arbitrary legacy rows or claim freshness.

Stored match reasons, decision, version and evaluation time are preserved. Revision,
run and acquisition scope remain unattributed. Delivery is a separate client/group/
destination fact. No destination means delivery state is Not reported. Freshness
remains Not reported because current legacy matches do not establish a verified
collected population. Historical suppression uses the existing exact tuple then
canonical-URL predicate. No group outcome is synthesized.

Outcome reads reproduce JOB-7's exact-subject version projection: latest explicit
event, then that historical row's baseline, otherwise Unknown. Imported status,
baseline, version and event identity remain separate. History includes imported
rows and recorded deliveries without synthetic jobs or copied surfacing flags.
A group summary's outcome is unavailable; posting members carry their own outcomes.

## Snapshots, errors and response safety

Snapshots retain ordered values and authorized details, not just IDs, for 30 minutes.
Random cursor handles bind operator, authorization version, client, route,
representation, filters, sort and limit. Cursor-only requests reuse bound parameters;
conflicting supplied parameters return INVALID_CURSOR. Detail with snapshot_id
uses captured values. Expired or lost snapshots return SNAPSHOT_EXPIRED, never
silently fresh data. Restart loses all handles. Member traversal may allocate a
new snapshot from the parent snapshot's captured member values.

Maximum five active snapshots per operator; quota excess returns 429 rather than
evicting a live traversal. Initial read admission is a token bucket of 120/minute,
burst 20, with at most ten concurrent requests. This is process-local: run one
worker in trusted development. It is unrelated to provider request budgets.

All authorized responses are `Cache-Control: no-store`, with server-generated
request IDs. Resource detail responses include opaque representation ETags; no
If-Match mutation behavior is enabled. Common metadata reports unknown observation/acquisition attribution
as null plus limitations; served time is not evidence time. Empty source-failure
arrays with unknown completeness do not assert healthy sources. Reported counts
include genuine zero; missing measurements remain null.

Errors use one typed envelope and allowlisted recovery classes, including 409
EVIDENCE_SCOPE_UNAVAILABLE (adjust scope) and 503 EVIDENCE_UNAVAILABLE (retry service).
Unexpected failures expose no exception text. SQL values are parameterized; query
identifiers are allowlisted. Normal detail excludes raw HTML/payloads and returns
provider descriptions only as JSON strings. The eventual frontend must output-encode
these strings.

Clickable destinations preserve exact stored URLs: safe vacancy-specific apply_url,
then canonical vacancy URL, otherwise unavailable. The initial policy permits
HTTPS only, rejects credentials, localhost/private literal IPs and generic careers
paths, and never synthesizes `/apply`. No DNS lookup, redirect following, provider
fetch or browser-supplied URL execution occurs on reads. The service does not claim
to authenticate arbitrary external DNS ownership.

## Verification and next boundary

```sh
.venv/bin/pytest -q tests/test_service.py
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
git diff --check
```

Service tests skip when the optional service extra is absent; with it installed
they run against temporary fixtures and in-process ASGI requests. Test dependencies
currently emit upstream Starlette/httpx and AnyIO deprecation warnings; no new HTTP
client dependency was added to suppress those warnings.

The next integration boundary is the same-origin BFF and replacement of frontend
fixture transport after review/publication. No frontend file or capability was
changed in JOB-32. Production auth and richer immutable run/revision evidence
remain explicitly unsupported, not hidden behind fabricated data.
