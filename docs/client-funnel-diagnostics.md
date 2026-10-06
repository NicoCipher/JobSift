# Client Funnel Diagnostics V1

Client Funnel Diagnostics explains where retained shared inventory stops qualifying
for a client. It is observability only: it does not change SearchBrief matching,
freshness, dedupe/history, quotas, employer policy, or delivery selection.

## Fresh 0-24 hour funnel

The `overall` and `by_source` slices use cumulative counts for jobs with a known
posting time no more than 24 hours old at the client evaluation timestamp:

1. `fresh_0_24h`
2. `title_matched_0_24h`
3. `target_market_survived_0_24h`
4. `remote_survived_0_24h`
5. `other_rules_survived_0_24h`

Each stage is a subset of the previous stage. The difference between adjacent
stages is therefore the deterministic primary loss bucket for that funnel order.
For example, `title_matched_0_24h - target_market_survived_0_24h` is the number
lost at the target-market gate even when a job also fails a later rule.

`target_market_review_0_24h` and `remote_review_0_24h` are subsets that survived
their stage only because the configured unknown-evidence policy requires review.
The final fresh cohort is partitioned into `confirmed_matches_0_24h`,
`needs_review_matches_0_24h`, and `rejected_0_24h`.

The funnel is computed from the existing authoritative `JobMatch`; it never runs a
second matcher or implements a parallel SearchBrief.

## Match age buckets

`match_age_buckets` partitions retained confirmed and reviewable matches by the
posting timestamp at the same evaluation time:

- `age_0_24h`
- `age_24_48h`
- `age_48_72h`
- `age_over_72h`
- `unknown_age`
- `invalid_time`

These buckets are diagnostic evidence for policy decisions. They do not make jobs
outside the configured freshness rule deliverable.

## Provider breakdown

`by_source` contains the same aggregate slice for each ATS provider. It is intended
to show which providers produce client-relevant yield, not merely raw posting
volume. No job titles, URLs, client IDs, Sheet identifiers, or other job-level
details are added to the public profile-run diagnostics.

## Delivery tail

The `delivery` section appends the existing authoritative batch accounting:

- history and prior-delivery suppression;
- practical-duplicate collapse;
- freshness, unknown-age, and invalid-time suppression;
- employer cooldown and per-employer cap suppression;
- fresh eligible groups and employers;
- selected count and shortfall.

This keeps the diagnostic path aligned with the real delivery path instead of
estimating downstream losses separately.
