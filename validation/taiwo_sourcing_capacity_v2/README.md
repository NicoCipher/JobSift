# Taiwo SearchBrief V2 offline capacity baseline (JOB-29)

This namespace freezes a new vocabulary experiment over already acquired evidence.
It does not continue the V1 Workday run, export deliveries, or make provider requests.

## Brief revision and operator identity

`config/search_briefs/taiwo_operator_sourcing_v2.json` contains exactly the accepted
Conservative V2 additions. Every other JSON field, including notes, is identical to
V1. V1's bytes remain frozen at
`7502c14a0dfc8ecdfaca9b2d1ba70a2c02aaec67aaf28bb495a5a7ba76d361f6`.

`brief_version = operator-style-sourcing-brief-v1` is the existing model/loader's
schema discriminator, not the client's vocabulary revision. Both configs use
that schema. The separate manifest identifies the vocabulary revision as
`taiwo-operator-sourcing-v2-conservative`, by filename and exact config SHA-256.
No extra unsupported model field or schema change is needed for this workflow.

The human/operator identity remains `taiwo_operator_sourcing_v1` despite its legacy
suffix. Renaming it would disconnect the following existing state:

| State | Existing identity contract | V2 treatment |
| --- | --- | --- |
| `historical_job_links` | client + provider identity, then client + normalized URL | Same client; all three operator statuses suppress |
| `historical_imports` | client + workbook hash | Same history; no re-import or synthetic Jobs |
| `historical_blacklist_evidence` | client scoped, separate evidence | Retained; no new suppression behavior |
| `job_matches` | primary key `(job_id, client_id)` | Recompute both briefs in memory; versioned hashes/evidence here |
| `posting_delivery_groups`, `delivery_keys` | global posting/group identity, independent of client | Reuse existing dedupe-v1 topology |
| `group_deliveries` | `(group_id, client_id, destination)` | Keep client AND cumulative destination |
| `exports` | `(job_id, client_id, destination)` | No export or marker writes in this baseline |

`SQLiteRepository.save_match()` replaces the current match for a job/client pair;
it cannot retain simultaneous revision histories in that table. V2 does not write
to either V1 DB. Its matching evidence is isolated here. A future V2 DB may hold
V2 matches while V1 databases remain immutable. A future requirement to keep all
brief revisions simultaneously in one production `job_matches` table would need
an explicit schema design; it is not required for this isolated baseline.

The generic pipeline uses the resolved CSV path as its destination. Merely picking
a new CSV path does NOT preserve destination-specific delivery suppression.
Future capacity continuation must retain the established cumulative destination:
`taiwo-sourcing-capacity-v1:stage-a-plus-stage-b`.
Versioned CSV files must be projections of that ledger, not new delivery identities.

## Frozen artifacts and reproduction

- `manifest.json`: schema/revision/client identities, exact V1/V2 config hashes,
  acquisition scope, hashes for inputs consumed by the calculation, production Python file hashes,
  unchanged matcher/dedupe versions, evaluator hash, and canonical manifest hash.
- `summary.json`: source counts, independently recomputed matching totals and
  deterministic matching-evidence hashes, historical suppression, group projection,
  14 incremental eligible posting identities and their V1/V2 reasons, 44 inherited
  delivery markers, and equivalent capacity.
- `workday_handoff.json`: the original 536-target order/coordinates with read-only
  acquisition dispositions. This is a design artifact, not a runnable live queue.

Run from the repository root:

```sh
.venv/bin/python -m validation.taiwo_sourcing_capacity_v2.run
```

The runner opens both existing SQLite files using `mode=ro` and `query_only=ON`.
It refuses missing inputs or journal/WAL sidecars, compares Stage A identities and
fingerprints to their Stage B copies, and rechecks every input hash before writing
new V2 outputs. Input drift or any JOB-28 result mismatch aborts the run. It consumes
the V1 Stage A summary for its frozen 44-group accounting; the unrelated V1 Stage B
summary is not an input and cannot affect reproduction.
Existing output files are verified byte-for-byte and never overwritten. There is
no network CLI mode and no repository initialization against a V1 database.

The JSON output contains no copied job-description archive or runtime database.
Full deterministic match hashes exclude evaluation timestamps. Their traversal
order is Stage A jobs by ID followed by Workday jobs by ID; each canonical JSON
record ends with a newline. The acquired payloads remain in their original local,
gitignored databases. Reproduction requires those exact bytes. Preserve an
immutable copy outside version control before any future process may change an
input; changing bytes requires a separate acquisition revision, not refreezing here.

## Capacity interpretation

Expected reconciliation: 48,710 postings (25,404 Greenhouse, 16,047 Ashby, 6,922
Lever, 337 Workday). V1 yields 48 eligible postings; V2 yields 62. The 14 new eligible
postings have no historical suppression and no previously delivered cumulative
group. Two duplicate postings collapse, leaving 12 additional fresh groups.

The V1 equivalent baseline is 44 previously selected fresh groups, so V2's equivalent
baseline from this acquired snapshot is **56**. This does not assert 56 new actual
exports. The V1 Stage A accounting remains separately recorded: 2 historical
identity suppressions, 0 URL suppressions, 2 already-delivered replay groups, and
0 within-target duplicate groups collapsed. The current full-corpus projection
checks against all 44 now-existing markers; these are different accounting windows
and must not be added together. Needs-review decisions remain ineligible.

The 337 already-acquired Workday postings contribute no eligible V2 additions.
The snapshot remains below 200 (144 remaining) and 250 (194 remaining). It does
not measure five-day capacity or justify a broader role vocabulary.

## Future Workday continuation design — not executed or authorized here

1. Preserve both V1 runtime databases and artifacts. Build an independent V2
   runtime from frozen acquired Jobs, history, dedupe topology, and cumulative
   delivery markers, verifying the manifest's input hashes first. Never point the
   old V1 Stage B runner at the V2 brief or update its rows.
2. Clear/omit copied `job_matches` in the NEW runtime; recompute all 48,710 matches
   with the exact frozen V2 config. Store V2 match results and their config hash
   under the V2 experiment identity. Do not relabel V1 results as V2.
3. Reconcile the existing 44 delivery markers. If the 12 baseline fresh groups are
   admitted to the future replay's delivery ledger, record them once as baseline
   selections in the NEW runtime, with explicit provenance (not actual sent
   deliveries). Do this atomically with the V2 baseline completion record. Report
   inherited 44, baseline incremental 12, and future acquisition contributions
   separately. CSV remains a projection. Actual operator delivery needs its own
   explicit action and must not be implied by this offline baseline.
4. Reuse the 14 successful Workday target acquisitions (337 Jobs, including any
   successful empty boards); do not download them again. Their acquisition is
   complete even though their matches must be recomputed for V2.
5. Preserve the 521 never-attempted targets in their frozen relative order and
   existing cost tiers. These are the normal future continuation candidates.
   Penn Mutual is a separate failed acquisition, not a successfully acquired board.
   Preserve its historical `parse_failure` and original error exactly. Keep it
   outside automatic continuation pending an explicit retry decision; JOB-26 will
   classify equivalent future network failures correctly. Nothing here retries it.
6. Freeze a separate continuation manifest binding this baseline, the V2 brief
   SHA, client, cumulative destination, target order, explicit failed-target policy,
   and unchanged 200/250 thresholds. The 200 business threshold is reporting only;
   250 stops after the completed target. Preserve bounded serial request budgets,
   deferred target semantics, completeness checks and SQLite transaction guarantees.
7. Test offline before authorizing any live work: cloned-state continuity, no
   re-download of successful targets, recomputed V2 matches, baseline idempotency,
   historical and delivered suppression, rollback, resumption, and capacity totals
   from committed V2 delivery rows. Future status must perform no network work.

No continuation runner or new production behavior is implemented by JOB-29.
