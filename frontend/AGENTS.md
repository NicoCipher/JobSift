# JobSift Frontend Agent Guide

Scope: everything under `frontend/`. The root `AGENTS.md` still applies.

Keep this UI a thin operator surface. It may present evidence and dispatch explicitly allowlisted server-side operations, but it must not become the authority for matching, freshness, dedupe, delivery history, quotas, or source admission.

## Boundaries

- Never expose GitHub, Neon/Postgres, Google, provider, or service credentials to the browser.
- Production mutations go through the existing guarded server routes and allowlisted GitHub Actions; do not add direct browser writes to Neon, Sheets, or ATS providers.
- Preserve Vercel Authentication and same-origin/fail-closed controls on production operations.
- Do not synthesize backend facts the service does not report. Unknown stays unknown.
- Keep opaque client/profile handles opaque; do not surface private client or Sheet identifiers.
- Prefer obvious mobile/operator states over hidden status text. Actions, selections, loading, failures, and next steps should be visible.

## Context discipline

Read `frontend/README.md` only when the task touches an interface or operational contract documented there. Do not preload all frontend files. Trace from the affected route/component to the minimum contracts and tests.

## Verification

While iterating, run the narrowest relevant Playwright test. For merge-ready frontend changes, run:

```sh
cd frontend
npm run lint
npm run build
npm test
```

Do not perform unrelated visual refactors while fixing an operational or correctness issue.
