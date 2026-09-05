# Posting identity and delivery groups

Every source posting remains a `jobs` row, uniquely identified by source, board,
and provider job ID. A delivery group means the operator should normally receive
one lead. It is not a replacement for posting provenance or a matching decision.

## Evidence contract (`dedupe-v1`)

1. **Same source identity:** update the same posting and preserve its ID and history.
2. **Exact normalized URL:** source-provided job, canonical, or application URLs may
   link postings. Existing tracking/fragment/trailing-slash normalization applies;
   query ordering is normalized, but meaningful parameter values remain. Generic
   home, `/jobs`, `/careers`, and `/apply` pages are excluded. This relies on the
   collector supplying vacancy-specific URLs, not shared board/landing URLs.
3. **Exact practical-vacancy evidence:** require all of these to agree:
   - Unicode-normalized, case-folded company and title, with collapsed whitespace;
   - the **entire** similarly normalized description, containing at least 40 words;
   - a nonempty identical country set and a known identical work mode;
   - identical location restrictions after removing redundant established country
     aliases and work-mode labels; city and region must also agree;
   - identical department and employment-type evidence (including missing values).

Forty words is a conservative minimum-evidence guard, not a similarity score. No
boilerplate, requirements, compensation, restrictions, or numbers are stripped.
Missing description/country/work-mode evidence disables level 3. Missing department
does not match a known department. Short or different descriptions stay separate
unless exact vacancy URL evidence connects them. Company/title alone never group.

Exact URLs are stronger evidence than differing content. Grouping is source-neutral;
the same rule can relate two ATS postings without merging their rows. Level 3 is
deliberately strict and will miss near duplicates whose full descriptions differ.

## Storage and migration

`posting_delivery_groups` maps each posting to `delivery_groups`; `delivery_keys`
stores indexed, versioned evidence for current postings. All original URLs, source
IDs, timestamps, fingerprints and payloads remain in `jobs`.

`group_deliveries` is unique by `(group_id, client_id, destination)` and records the
selected posting. Existing `exports` remains the append-only posting-delivery audit.
Groups joining later transfer delivery suppression; old export rows remain intact.
Group IDs use a stable source-derived UUID and deterministic minimum on joining.

Opening a valid older database removes only the unique canonical-URL index, adds
the group tables/indexes, backfills groups and converts export history to group
delivery records. It does not rewrite jobs, matches or historical exports/CSV rows.
Repeated opening is idempotent. Migration uses a write transaction and foreign keys
are enabled on **every** connection. Existing orphaned records stop migration with
an actionable error; missing source provenance cannot safely be fabricated.

The pre-fix exact-URL regression produced one persisted posting for two source IDs
and an orphaned `job_matches` row. The initializer's `PRAGMA foreign_keys` applied
only to its own connection. Both regressions now verify preservation and integrity.

## Delivery and representative selection

The pipeline persists and matches all received postings before selecting deliveries.
Only matching postings returned by the current successful/partial collection may be
selected; an old stored posting is not assumed to be currently live. Offline replay
uses snapshot evidence and makes no new availability claim.

Within that eligible set, selection uses this stable order:

1. Source-provided direct application URL present.
2. Longer normalized description, then more structured evidence fields.
3. Newer source update timestamp, then newer publication timestamp.
4. Source, board, provider ID and posting ID as lexical tie-breakers.

Provider-distinct members are all matched; one representative per undelivered group
is exported. Missing a preferred URL does not discard alternate URLs. Previously
delivered groups are not re-exported when richer members arrive. Client and resolved
CSV destination each retain independent delivery state. Historical groups persist
across source edits; current evidence keys are refreshed, but delivery identity is
not reset. There is no automatic group splitting or correction of an earlier false
grouping in this pass. Group joining is transitive, so source URL correctness matters.

SQLite is authoritative. CSV remains the exact five-column append-only surface.
Concurrent CSV writers and a crash between CSV append and delivery recording remain
known limitations; this pass does not claim atomicity across SQLite and a file.

## Frozen regression and offline evaluation

`tests/fixtures/gitlab_government_support.json` contains the two preserved canonical
postings (HTML/raw metadata omitted only from the test fixture). The source was
`validation/coverage/gitlab.sqlite3`, SHA-256
`e75efec1a71ce42954d38ce8f13254cb32dd09334260f48f0857cf4626e03891`.
The descriptions are identical, with 10,253 characters each. Both provider IDs stay
persisted; the newer published posting `8707353002` is selected over `8628780002`.

Run `python -m validation.delivery_groups_v1.replay_offline` with the externally
preserved v5 databases at the locations in `operator_style_v1/results/board_metrics.csv`.
The script verifies input hashes and the frozen brief, asserts deterministic-v5,
uses fresh destinations, and compares every match decision, score and reason with
the original. It refuses to overwrite results. No live source calls are made.
Large databases remain external; committed summaries retain their input/output hashes.
