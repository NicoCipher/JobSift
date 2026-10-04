# JobSift operator frontend — JOB-31

## Production Operations control

`/operations` is a separate production control surface. It does not turn the
read-only evidence API into a mutation API. Instead, same-origin server routes
dispatch a strict allowlist of existing GitHub Actions workflows:

- `refresh-live-inventory.yml` for guarded manual refreshes;
- `client-delivery-control.yml` for serialized delivery and registered client-Sheet controls.

The browser never receives GitHub, Turso, Google or provider credentials. Set a
server-only `JOBSIFT_GITHUB_TOKEN` with Actions write access to
`NicoCipher/JobSift` to enable commands. Without it, workflow status remains
visible and every production command is disabled/fails closed.

The deployed Vercel project must remain protected by Vercel Authentication for
all deployment targets. Do not treat a shareable deployment-protection bypass
link as an operator session. POST controls also reject cross-origin requests.

Manual inventory refreshes preserve the existing workflow contract: choices are
limited to Workday targets 1/5/10/20/25 and detail concurrency 4/6/8, and manual
runs do not advance the scheduled logical-cohort cursor. Scheduled production
remains 25 Workday targets/hour. Delivery and Sheet commands go through the existing
`jobsift-client-delivery-mutation` queue; the frontend does not write Turso
directly. Client-Sheet controls intentionally use only an opaque delivery-profile
ID: operators can check, disable, or verified-re-enable an already registered
Sheet without exposing spreadsheet IDs, tab names, client IDs, or Google
credentials through public workflow inputs. Registering a brand-new physical
Sheet remains a private provisioning operation until a private registration
transport is added.


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
development and its browser tests. In live mode, only Jobs and presentation
Settings are exposed; other views are marked unavailable until their service
contracts are integrated. No mutation, sourcing command, or production login is
enabled. An unregistered destination or unavailable group representation
returns the service error rather than substituting fictional rows. The service
requires a provisioned database and explicit private catalogue before the live
view can show jobs.

A read-only Next.js App Router slice. The default mode uses explicitly fictional development evidence. The primary work surface is `/jobs`; `/` redirects there. The optional local service connection reads registered evidence through the Python operator service. It does not enable sourcing, writes, or production authentication.

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

Navigation: Dashboard, Operations, Jobs, Review, Search Briefs, Runs, History, Diagnostics, Settings. Secondary routes are deliberately small read-only evidence views. Settings persist only theme, density and character-shortcut preferences.

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
