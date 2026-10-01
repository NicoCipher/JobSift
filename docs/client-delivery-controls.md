# Client delivery controls

JobSift separates **sourcing** from **client delivery**.

The shared inventory is refreshed automatically. A client delivery profile then
controls which registered Google Sheet receives matching jobs and how many may
be delivered during that client's local day.

## Delivery profile

Each profile is identified by:

- `client_id`
- `destination_id` — a previously registered client-owned Google Sheet
- `sourcing_plan_id` — binds the client SearchBrief
- `daily_quota` — maximum links for that destination per client-local day
- `status` — `active` or `paused`
- `delivery_mode` — `review` or `auto`
- `timezone` — IANA timezone used for daily quota accounting

The physical spreadsheet URL is never used as delivery identity. The stable
registered destination ID remains the duplicate-history boundary.

## Automatic sourcing

`Refresh Live Job Inventory` runs hourly at minute 17.

Each run:

1. selects a rotating 100-target cohort from the production-approved ATS registry,
2. collects the cohort in parallel shards,
3. validates the complete shard artifact set before persistence,
4. persists the shared inventory to Turso,
5. prunes full payloads beyond the 72-hour retention window,
6. reconciles every active client's existing Sheet links,
7. evaluates the retained shared inventory against each client's SearchBrief,
8. prepares or publishes only the remaining daily quota.

The cohort rotates automatically, so JobSift does not wait for an operator to
start sourcing before fresh jobs can exist.

SearchBrief quality rules are not changed to satisfy quota. Posting freshness,
unknown-age rejection, matching, practical dedupe, historical suppression and
the per-employer cap remain authoritative.

## Existing Sheet rows

Before preparing a client batch, JobSift reads the registered Sheet and records
every existing value in the mapped `Job Link` column as prior surfacing.

This prevents a manually added or previously delivered link from being
rediscovered and appended again. Existing links first observed during the
current client-local day also count toward that day's quota, without
double-counting links already present in the delivery journal.

If the Sheet ID, tab identity or registered header has drifted, delivery fails
closed.

## Phone control

Because the repository is public, client IDs and Sheet destination IDs are
never accepted as public workflow inputs.

### One-time profile setup

Open **GitHub → Actions → Configure Client Delivery Profile → Run workflow**.

This one-time action reads the existing private
`JOBSIFT_CLIENT_SHEET_REGISTRATION_JSON` secret, registers or refreshes that
Sheet destination, creates the delivery profile, reconciles links already on
the Sheet, and returns an opaque 16-character `profile_id`. The public inputs
contain only non-sensitive settings such as quota, delivery mode and timezone.

### Routine controls

Open **GitHub → Actions → Client Delivery Control → Run workflow** and use the
opaque `profile_id`.

Available operations:

- **status** — reconcile the Sheet, then show quota, delivered today, remaining today, mode and state
- **list** — show the same safe status for all profiles, without client/Sheet IDs
- **pause** — stop new preparation/delivery
- **resume** — reactivate the profile
- **set-quota** — change the daily maximum
- **set-mode** — switch between review and auto delivery
- **set-timezone** — change the client-local quota timezone
- **run-now** — consume current shared inventory for that profile immediately
- **release-batch** — publish an explicitly reviewed prepared batch
- **discard-batch** — discard an unreleased prepared batch

The opaque profile ID is an operator handle, not a client identifier or secret.

### Review mode

`review` is the safe default. JobSift prepares a frozen batch and waits for an
explicit release. While a prepared batch is unresolved, it will not prepare
another batch for that destination.

### Auto mode

`auto` publishes a valid prepared batch automatically after reconciliation.
Use this only after the client's SearchBrief and destination mapping are proven.

## Quota behaviour

The quota is a daily ceiling, not "rows per run".

Example: a client target is 100/day and 14 links have already appeared on the
Sheet today. The next delivery request is capped at 86. If only 20 qualifying
fresh jobs exist, JobSift may deliver/prepare 20; later refreshes can continue
toward the remaining quota.

JobSift never pads a shortfall with stale, unknown-age, hybrid/on-site or
otherwise non-qualifying jobs.

## Onboarding another client

1. Create and verify the client's SearchBrief/sourcing plan.
2. Put that client's private registration JSON into
   `JOBSIFT_CLIENT_SHEET_REGISTRATION_JSON`.
3. Share the Sheet with the JobSift service account.
4. Run **Configure Client Delivery Profile** once; it registers the destination,
   reconciles existing links, and returns the opaque profile ID.
5. Use **Client Delivery Control** for quota, pause/resume, mode and run-now.
6. Leave the shared inventory refresher running.

A single client may have more than one registered destination. Each destination
gets its own delivery profile and quota.
