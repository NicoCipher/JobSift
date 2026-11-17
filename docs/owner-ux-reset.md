# Owner experience reset

## Product promise

JobSift is an owner-operated job sourcing and delivery service. The owner should be able to configure clients, understand whether useful jobs are being found, review prepared matches, and deliver approved jobs without learning the internal engineering system.

## Primary owner journey

1. **Home** — understand whether sourcing and delivery are healthy, what actually happened, and the one next action.
2. **Clients** — add a client, define the search criteria, register the destination Sheet, and manage pause/quota/review settings.
3. **Review** — inspect prepared jobs, understand why they match, remove unsuitable results, and approve delivery.
4. **Settings** — change only owner-facing preferences and essential account settings.

Technical operations, diagnostics, scheduler controls, provider details, run identifiers, and recovery tools stay in a clearly labelled Advanced area.

## Non-negotiable product behavior

- Do not display invented or fixture counts as production results.
- Distinguish jobs discovered, jobs matched, jobs waiting for review, and jobs actually delivered.
- Unknown or incomplete backend state must be labelled unknown; never render it as zero or success.
- Never trigger sourcing, delivery, or expensive synchronization simply by opening a page.
- Preserve owner authentication, same-origin mutation routes, backend matching/freshness/deduplication, quota checks, and safe Sheet-write recovery.
- Do not expose credentials, Sheet identifiers, provider internals, profile IDs, or database details in the browser.
- Keep the UI usable at 360px mobile width without horizontal scrolling.

## Delivery acceptance journey

A release is not considered usable until an owner can complete this path with real production evidence:

1. Open the owner home and understand the current state.
2. Create a client using plain-language steps.
3. Save the client's criteria and registered Google Sheet destination.
4. Run or wait for shared inventory sourcing without accidentally starting duplicate work.
5. See how many fresh, unique, eligible jobs matched that client's criteria.
6. Review the prepared jobs and understand why each one is included.
7. Approve the batch and verify the rows actually reached the correct Sheet.
8. Return later and understand what was delivered, what remains pending, and whether any action is required.

## Scope of the first UI pass

- Reduce visual density on the owner home.
- Make the primary status, next action, and verified customer outcomes visually dominant.
- Keep rare controls visually secondary.
- Retain all existing server-side safety and authorization boundaries.
- Do not change database behavior or increase scheduled sourcing workload as part of the UI work.

## Required verification before release

Test normal, failed, incomplete/unknown, empty-client, paused-client, pending-review, and uncertain-delivery states. Verify all actions reach the existing guarded routes. Test at 360px mobile and desktop, keyboard navigation, and light/dark themes. Run frontend lint, build, and Playwright tests. Obtain an independent code review; do not merge with failing checks.
