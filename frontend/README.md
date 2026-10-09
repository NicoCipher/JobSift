# JobSift operator frontend — JOB-31

## Owner-only access (public GitHub repository)

**Do not merge or deploy the owner-auth change before configuring BOTH secrets.**
Keep Vercel Authentication enabled for all deployment targets. Vercel shareable
links and automation bypasses are not owner identity checks.

An independent server-side gate protects the operator pages and ALL control,
status, review, onboarding, Sheet, and local evidence endpoints, including GET.
A signed 24-hour, HttpOnly, Secure (HTTPS), SameSite=Strict owner-session cookie
is required. The sign-in form is at `/owner-login`. Owner auth fails closed
on production builds, Vercel, and operator-mode dev sessions. Local isolated
fixture development retains existing behavior unless the owner gate is forced.

Server-only Vercel environment variables (production AND any protected preview
environment that serves the operator app):

- `JOBSIFT_OWNER_ACCESS_KEY_SHA256`: lowercase hexadecimal SHA-256 digest of
  an independently generated 32-byte **random base64url owner key**. Store the
  RAW key in your password manager, NEVER in Git, Vercel public env, or logs.
- `JOBSIFT_OWNER_SESSION_SECRET`: a second independently generated random
  32-byte base64url secret, different from the login key.

To generate values locally in a **private terminal** (do not copy output to
an issue, PR, or chat), run:

```sh
node -e "const c=require('node:crypto'); const k=c.randomBytes(32).toString('base64url'); console.log('PRIVATE_LOGIN_KEY='+k); console.log('JOBSIFT_OWNER_ACCESS_KEY_SHA256='+c.createHash('sha256').update(k).digest('hex')); console.log('JOBSIFT_OWNER_SESSION_SECRET='+c.randomBytes(32).toString('base64url'))"
```

Paste only the digest and the session secret into Vercel **server-only**
environment settings. After deployment, Vercel Authentication prompts for
Vercel sign-in and JobSift then prompts for the private login key. Remove any
old Vercel shareable/bypass links that are not required. Rotating the session
secret invalidates existing app sessions immediately; rotating the owner key
prevents future logins with the old key.

No external identity provider is used for this lightweight second factor: the
32-byte key is the authorization credential. Anyone who steals it AND accesses
the site could impersonate the operator. Treat the key like a password and
never distribute it. For identity-bound MFA, replace this gate with an audited
provider (e.g. Auth.js with GitHub OAuth restricted to one stable GitHub user ID).

## Production Operations control

`/operations` is a separate production control surface. It does not turn the
read-only evidence API into a mutation API. Instead, same-origin server routes
dispatch a strict allowlist of existing GitHub Actions workflows:

- `refresh-live-inventory.yml` for guarded manual refreshes;
- `client-delivery-control.yml` for serialized delivery and registered client-Sheet controls.

The browser never receives GitHub, Neon, Google or provider credentials. Set a
server-only `JOBSIFT_GITHUB_TOKEN` with Actions write access to
`NicoCipher/JobSift` to enable commands. Without it, workflow status remains
visible and every production command is disabled/fails closed.

The deployed Vercel project must remain protected by Vercel Authentication for
all deployment targets. Do not treat a shareable deployment-protection bypass
link as an operator session. POST controls also reject cross-origin requests.

Legacy/static production client labels and opaque profile handles may still come
from the server-only `JOBSIFT_OPERATOR_PROFILES` allowlist. Frontend-created
clients instead come from the durable `operator_clients` record in Postgres.
Only an authoritative operator-managed snapshot may mint a short-lived
profile-bound control capability, so an arbitrary backend profile does not become
actionable merely because a browser submits its opaque handle. Example legacy
allowlist:

```json
[
  {
    "profile_id": "0123456789abcdef",
    "client_name": "ACME",
    "destination_name": "ACME Jobs"
  }
]
```

The variable contains no Sheet URL, spreadsheet ID, client ID, or credentials.
When that legacy allowlist is absent, static profiles remain fail-closed, while
durable operator-managed clients may be controlled only with the short-lived
server-signed capability issued from authoritative state. Development retains the
authored fixture catalogue for tests. Dispatch and review routes enforce either
the explicit legacy allowlist or a valid dynamic-client capability, so an
authenticated browser cannot make an arbitrary opaque profile actionable.

Manual inventory refreshes preserve the existing workflow contract: choices are
limited to Workday targets 1/5/10/20/25, detail concurrency 4/6/8, and a
yield-aware bonus budget of 0/25/50/75/100 targets. The bonus is applied only
after the oldest-due fairness floor, so a manual comparison cannot starve due
sources. Manual runs do not advance the scheduled logical-cohort cursor.
Scheduled production uses 25 Workday targets and a 100-target yield bonus. Delivery and Sheet commands go through the existing
`jobsift-client-delivery-mutation` queue; the frontend does not write Turso
directly. Client-Sheet controls intentionally use only an opaque delivery-profile
ID: operators can check, disable, or verified-re-enable an already registered
Sheet without exposing spreadsheet IDs, tab names, client IDs, or Google
credentials through public workflow inputs.

Brand-new client provisioning is available at `/clients/new`. The same
32-byte base64url `JOBSIFT_PROVISIONING_KEY` must be configured as a
server-only Vercel environment variable and GitHub Actions repository secret
before onboarding is enabled. The Vercel route AES-256-GCM encrypts the client
criteria and Sheet registration payload before dispatch; Actions receives
ciphertext only and decrypts it inside the trusted runner. If either side lacks
the key, Add Client fails closed instead of falling back to plaintext.

Physical Google Sheet identifiers are also encrypted before operator snapshots
are written to public workflow logs. The Vercel status route decrypts that
server-side and gives the browser only a short-lived opaque Sheet handle.

## Normal operator workflow

Production operators land on `/clients`. The normal path is now:

`Clients → Add client → Find jobs → Review → Send to client Sheet`

`/clients` shows the human-readable client name, active/paused state,
review/auto mode, daily limit, sent-today count, waiting review count, Sheet
readiness and the latest client match result. `/clients/new` guides the operator
through Client → Criteria → Delivery → Google Sheet → Confirm. Client-local
daily quota timezones accept any valid IANA timezone and onboarding cannot exceed
the 2,000-links-per-client/day product ceiling. Opaque profile, batch,
spreadsheet and worksheet IDs remain server-side implementation details.

`/review` reads the exact prepared batch through a server-only review bridge.
The browser does not recompute matching. JobSift only shows match explanations
when the current authoritative job/match evidence still hashes to the frozen
prepared-batch evidence. Changed or stale evidence is visibly unsafe and blocks
release until removed or refreshed.

Keep/Remove decisions are applied through a generation-bound
`release-selection` backend operation. Removed rows are journaled as
`operator_removed`; the remaining rows still pass the existing release guard
for freshness, quota, history/dedupe, profile state, destination state and
uncertain Sheet-write recovery immediately before delivery.

`/operations` remains the advanced/recovery surface rather than the normal
operator workflow. GitHub Actions are transitional server-side machinery and are
not modeled in the normal UI.


## Production operator mode

Vercel production uses `NEXT_PUBLIC_JOBSIFT_OPERATOR=1` and keeps
`NEXT_PUBLIC_JOBSIFT_LIVE=0`. Operator mode is a presentation/routing switch
only; it is not an authorization mechanism. Vercel Authentication remains the
outer authenticated boundary, and all mutations still pass through the
same-origin server routes and backend guards.

Operator mode does not create or depend on a fictional session/client. The shell
shows only Clients, Review, Operations and Settings. Direct access to the legacy
fixture Jobs route redirects to Clients so development evidence cannot be
mistaken for production data.

`NEXT_PUBLIC_JOBSIFT_LIVE=1` has a different meaning: it is reserved for the
loopback Python operator-service workflow described below and requires
`JOBSIFT_SERVICE_URL=http://127.0.0.1:<port>`.

## Local service connection

The Jobs view can read registered evidence from the Python operator service. Start
the service with a private `JOBSIFT_SERVICE_CONFIG` as described in
`docs/api/operator-service-runtime-v1.md`, then start the frontend with:

```sh
NEXT_PUBLIC_JOBSIFT_LIVE=1 JOBSIFT_SERVICE_URL=http://127.0.0.1:8000 npm run dev
```

Both processes must bind to loopback. The browser calls a same-origin, GET-only
route; the service URL remains on the server. The route accepts only local
`127.0.0.1` HTTP endpoints. The session, client, group list and group detail
come from the service. In local service mode, `/jobs` shows individual postings,
including postings without a recorded group representative; `/jobs?view=groups`
shows only service-backed delivery groups. These are distinct resource views and
no group is synthesized for a posting. The existing fixture mode remains the default for UI
development and its browser tests. In live mode, Clients, Review, Operations, Jobs and presentation Settings are
available. Clients/Review/Operations use the server-only production control
boundary; Jobs remains the registered-evidence view. No mutation, sourcing command, or production login is
enabled. An unregistered destination or unavailable group representation
returns the service error rather than substituting fictional rows. The service
requires a provisioned database and explicit private catalogue before the live
view can show jobs.

The default development mode still uses explicitly fictional evidence for the
legacy Jobs views. In production operator mode and local-service mode, `/`
redirects to `/clients`; in fixture development mode it redirects to `/jobs`. Production mutation routes
remain server-side and fail closed when their credentials/catalogue are absent.

## Run and verify

Use Node.js 24 and npm (verified with Node 24.16.0 / npm 11.13.0):

```sh
cd frontend
npm ci
npm run dev
```

Open <http://127.0.0.1:3000/jobs>. The server binds to loopback. Production preview: `npm run build`, then `npm start`.

```sh
npm test
npm run lint
npm run build
```

Tests use Playwright against locally installed Google Chrome (`channel: chrome`); install Chrome before running them. Playwright starts the local dev server if none exists. Lint includes TypeScript checking. No root configuration changes or Python dependencies are required.

Runtime: Next.js 16.3.4, React / React DOM 19.3.0. Tooling: TypeScript 6.0.3, ESLint 9.39.5, eslint-config-next 16.3.4, Playwright 1.63.0. TypeScript 7 and ESLint 10 were available but incompatible with the installed Next lint tooling; the compatible stable major versions are pinned. The npm lockfile records exact resolutions.

## Boundaries

- `lib/contracts/service.ts`: typed subset of the [service contract](../docs/api/operator-service-contract-v1.md), including explicit missing facts, units, scope, snapshots, capabilities and exact matcher decisions.
- `lib/api/interface.ts`: `JobSiftApi`, consumed by pages and components. `client.ts` selects the fixture or local service adapter. The GET-only route in `app/api/operator` forwards local reads.
- `lib/api/fixture-api.ts`: adapter-only filtering, stable authored order, opaque cursor pagination and 30-minute in-memory snapshots. The UI never calculates match, grouping, suppression or freshness. Browser Back retains the cursor scope within the running fixture session. Reload starts a new fixture session; expired cursors require an explicit refresh.
- `lib/fixtures/evidence.ts`: authored examples, including unknown and unavailable evidence, measured zero, partial coverage, prior delivery, historical suppression, and disabled mutations. Example application links use reserved example domains and do not submit applications.
- `lib/display.ts`: labels, null presentation, safe external-link handling and keyboard helpers.
- `components/`: shell, Jobs table/detail, status presentation and browser-local appearance preferences.
- `styles/tokens.css`: shared [operator UI standard](../docs/frontend/operator-ui-standard.md) tokens. `global.css` implements shell, table and responsive layout using those tokens.

The frozen 62 eligible postings / 56 equivalent fresh groups example is separate from the 52 authored Jobs groups. The default Strong/Possible scope reports 44 groups. No baseline count is computed from these rows. Needs review and Rejected examples retain previously recorded representatives and remain non-delivery-eligible. Saved match revision/run associations remain unreported where no evidence exists.

The API subset exposes convenience reads for one registered example brief, run and diagnostics cohort. It does not implement the complete service resource catalog, authorization, transport failures, persistence or resource selection. A future BFF must enforce the published contract; the fixture adapter is not a production service.

## Work surfaces

Navigation prioritizes Clients, Review and Operations for the production
operator workflow, with Jobs and legacy evidence surfaces still available where
enabled. Secondary routes are deliberately small read-only evidence views. Settings persist only theme, density and character-shortcut preferences.

Desktop uses a 48px top bar, 200px navigation and a 400px contextual detail pane where room permits. At narrower widths detail becomes the full work surface with a URL-addressable selection. Mobile rows retain the primary facts; all evidence is available in detail. No bulk-selection checkboxes or unsupported mutation controls are rendered.

Keyboard: `/` focuses visible Jobs search; `j` / `k` move among job links without wrapping while the list is active; native Enter opens detail; Escape closes it and returns focus. Inputs, editable elements, modifier shortcuts and dialogs guard character shortcuts. Native table semantics, disclosure controls, visible focus and labeled inputs form the accessibility foundation. Browser tests cover 320, 768, 1024 and 1440 widths, focus restoration, themes, pagination and evidence presentation; they do not certify screen-reader or WCAG conformance.

## JOB-31 file inventory

All 30 files below are new; no existing tracked file was modified for JOB-31. Generated dependencies, build output and test results are ignored.

```text
frontend/.gitignore
frontend/README.md
frontend/app/[section]/page.tsx
frontend/app/error.tsx
frontend/app/jobs/page.tsx
frontend/app/layout.tsx
frontend/app/not-found.tsx
frontend/app/page.tsx
frontend/components/job-detail.tsx
frontend/components/jobs-workspace.tsx
frontend/components/preferences.tsx
frontend/components/shell.tsx
frontend/components/status.tsx
frontend/eslint.config.mjs
frontend/lib/api/client.ts
frontend/lib/api/fixture-api.ts
frontend/lib/api/interface.ts
frontend/lib/contracts/service.ts
frontend/lib/display.ts
frontend/lib/fixtures/evidence.ts
frontend/next-env.d.ts
frontend/next.config.ts
frontend/package-lock.json
frontend/package.json
frontend/playwright.config.ts
frontend/styles/global.css
frontend/styles/tokens.css
frontend/tests/contracts.spec.ts
frontend/tests/workbench.spec.ts
frontend/tsconfig.json
```
