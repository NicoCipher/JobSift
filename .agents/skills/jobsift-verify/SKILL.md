---
name: jobsift-verify
description: JobSift-specific verification and review workflow. Use before PR/merge, after broad or risky changes, for CI failures, workflow failures, review findings, database changes, security-sensitive changes, or when asked whether a change is ready.
metadata:
  origin: JobSift
---

# JobSift Verify

Verify the changed surface, not the entire repository by reflex.

## 1. Scope first

Inspect the base/head diff, changed files, and relevant tests. Confirm the diff matches the requested intent and contains no unrelated cleanup.

## 2. Select checks by surface

**Python/backend**
- Run the narrowest relevant pytest selection first.
- Run `ruff check .` for merge-ready Python changes.
- Run the full CI pytest gate when the change is broad, cross-cutting, persistence-sensitive, or ready to merge.

**Persistence / delivery**
- Verify idempotency, duplicate/history behavior, retry/crash recovery, transaction boundaries, and SQLite/Postgres parity where applicable.
- For large-data changes, use the existing capacity/production-like tests rather than toy-only evidence.

**Frontend**
- Follow `frontend/AGENTS.md`.
- Prefer targeted Playwright tests while iterating; use the full frontend gate for broad or merge-ready changes.

**GitHub Actions**
- Inspect only the touched workflow and its contract tests.
- Check permissions, secret exposure, dispatch inputs, concurrency/serialization, timeouts, artifacts, and fail-closed behavior.

**Security-sensitive paths**
- Check credential boundaries, input validation, authorization, log/error leakage, and unsafe external writes.

## 3. Review independently

Review for correctness, data integrity, hidden behavior changes, performance regressions, missing edge cases, and missing tests. Do not approve a change because CI is green.

For CI failures, read the failing step and relevant log section first. Do not dump or reread complete logs unless the failure cannot be localized. Do not rerun a real failure as if it were flaky.

## 4. PR readiness

Before calling a PR ready, verify:

- head is based on the intended current main or conflicts are understood;
- required checks are green;
- unresolved review findings are fixed or explicitly accepted;
- user-visible or operational behavior is documented when needed;
- no product invariant was weakened to make tests or throughput pass.

Report **READY** or **NOT READY**, followed only by concrete blockers and the evidence checked.
