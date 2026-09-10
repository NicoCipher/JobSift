# JobSift operator UI standard

Authoritative product/design contract · JOB-30 · 10 September 2026

## 1. Authority and scope

This document governs future operator interfaces. **Must** denotes an acceptance requirement; **should** denotes a default that requires a documented reason to depart. It defines design, not an implemented frontend or an authorization to begin JOB-3. No component library, CSS framework, API, or backend change is prescribed.

When this document describes future interactions, their visibility and execution depend on an authoritative backend capability. A design requirement is never evidence that a capability exists. Preserve unavailable information as unavailable; never simulate successful actions.

Repository evidence consulted:

- [Domain models](../../job_scout/domain/models.py), [matcher](../../job_scout/matching/matcher.py), and [brief creation](../../job_scout/search_brief.py).
- [Sourcing plans](../sourcing_plans.md) and [run report model](../../job_scout/sourcing_plan.py).
- [Delivery groups](../delivery_groups.md) and [historical operator state](../historical_operator_state.md).
- [V1 brief](../../config/search_briefs/taiwo_operator_sourcing_v1.json), [V2 brief](../../config/search_briefs/taiwo_operator_sourcing_v2.json), and [V2 capacity interpretation](../../validation/taiwo_sourcing_capacity_v2/README.md).

If implementation evidence disagrees with a proposed display, preserve the evidence and resolve the contract before shipping. Version this document through ordinary review; do not create competing page-specific design systems.

## 2. Personality and principles

| Must feel | Practical meaning |
| --- | --- |
| Precise | Counts name their unit, scope, revision, and observation window. |
| Calm | Stable layouts, quiet neutrals, no celebratory feedback or urgency without cause. |
| Deliberate | Every column, control, and emphasis supports a task. |
| Trustworthy | Unknowns, partial coverage, and historical evidence stay visible. |
| Efficient | Dense rows, predictable keyboard actions, and immediate access to reasons. |

Never feel **flashy** (decoration dominates), **toy-like** (oversized controls and playful status), **noisy** (competing accents), **generic** (interchangeable KPI/card template), or **opaque** (unexplained decisions).

Quality comes from typography, alignment, proportion, spacing, hierarchy, excellent states, and restrained feedback. Borrow the consistency of mature developer tools, their direct navigation and progressive disclosure; do not reproduce another product's layout or brand.

Decisions and rationale:

1. Tables and divided sections are the default. Operators compare many records; repeated cards waste space and obscure shared columns.
2. One blue accent establishes navigation and action, not a score or health claim. Semantic color is subordinate to words.
3. A stable shell and explicit client scope prevent accidental cross-client interpretation.
4. Reasons are adjacent to decisions; deeper provenance is one disclosure away. Trust must not require reading raw JSON.
5. Paginated, stable result sets preserve position and auditability. Endless feeds and live reordering undermine deliberate review.
6. System typography and modest geometry minimize visual fashion and font dependency. The design should remain appropriate in 2031 without a visual reset.

Prohibited: glassmorphism, gratuitous gradients, glows, huge rounded cards, excessive shadows, decorative blobs, gradient text, giant application heroes, AI-dashboard clichés, rainbow badges, icons beside every label, excessive pills/cards, unnecessary illustrations, over-animation, novelty that slows scanning, and marketing-page styling inside the workbench.

## 3. Truth, identity, and capability boundaries

| Concept | Required presentation and guardrail |
| --- | --- |
| Operator / client | The signed-in operator, when authentication exists, and the client being sourced for are separate labels. Persist the backend client identifier across brief revisions; never derive a new client from a revision name. Do not imply an existing account/permission system. |
| SearchBrief revision | Show revision label plus its exact file/hash or future persisted revision ID. Both current V1 and V2 use `operator-style-sourcing-brief-v1`; schema version is not sourcing revision. V2 retains the existing client ID. Never infer active revision from highest number or filename alone. |
| Active brief | Name the brief explicitly bound to the selected plan/run or a future backend active pointer. If there is no active pointer, say “Brief used by this plan” or “Active brief unavailable.” |
| Rules | Render `must`, `prefer`, `avoid`, `ignore` as Required, Preferred, Excluded, Ignored. Unknown policies stay visible. Missing a preference does not reject a job. Residence is informational; market and explicit work eligibility are separate. |
| Match | Preserve strong_match → Strong match, possible_match → Possible, needs_review → Needs review, reject → Rejected. Never recompute matching, eligibility, ranking scores, or confidence in the browser. Needs review is currently not delivery-eligible. |
| Posting / group | One primary Jobs row represents a backend practical delivery group when that grouping is available. Inspect every member posting and its own decision. Use the backend representative; never group by title/company in the UI. If group data is unavailable, explicitly show a posting table and posting counts. |
| Delivery | Historical suppression is client-scoped; recorded group delivery also depends on destination. Include destination in delivery filters/context. New-to-storage, unseen-by-operator, eligible, and fresh-for-delivery are different facts. |
| Historical outcome | Applied / Not Applied / unknown are operator evidence, not matcher decisions. Imported blank and Not Applied rows were still surfaced and may suppress delivery. Opening a URL does not mark Applied, Seen, or delivered. |
| Availability | First seen / last seen are observations, not publication or closing dates. Source failure and absence do not establish that a job closed. Offline snapshots make no live availability claim. |
| Plans / runs | Plans say where to look; briefs say what to seek. Current final run statuses are success, partial, failure. Running, Paused, and Canceled require future authoritative lifecycle support. |
| Missing metrics | Current run reports include received, new, changed, unchanged, matched, rejected, exported and target outcomes. Separate normalized totals, suppression breakdowns, live progress, costs, limits, and shortfall require additional evidence. Show “Not reported,” never zero or inferred progress. |
| Capacity | “56 fresh groups in the current V2 equivalent baseline” describes a frozen acquired snapshot, not 56 new exports, live stock, daily output, or five-day capacity. Link the baseline artifact and its acquisition window. |

Capacity detail must retain: 48,710 acquired postings; V1 48 eligible postings; V2 62 eligible postings; 14 additional eligible postings; two duplicate postings collapse to 12 additional fresh groups; 44 + 12 = 56 equivalent fresh groups. A selected target of 200 implies a baseline gap of 144; 250 implies 194. Label these **baseline gaps**, not live run shortfalls. Never add suppression counts from different accounting windows. Current actual fresh deliveries require their own run/destination evidence.

All aggregate displays include units (postings, groups, targets, requests), scope (client, brief, run/cohort, destination as applicable), observation time, and completeness. Use “Not reported” for absent measurements, “Unknown” for reported unknown facts, “None” for a known empty set, and 0 only for a measured zero. A frontend may format or sum disjoint authoritative counts, but must document the calculation and never infer a business decision.

Reserved future operations: run execution/pause/cancel, brief revision persistence/activation, in-app outcome updates, review dispositions, bulk mutation, and permissions. Until backed by a verified contract, omit action controls and explain the limitation at the relevant surface. Read-only evidence inspection remains usable. “Relevant / Not relevant” must never silently rewrite matcher decisions or be substituted for Applied / Not Applied.

## 4. Shell and information architecture

Desktop shell: 48px top bar; 200px left navigation; main area uses 24px gutters at laptop width and 32px on large desktop. Top bar contains JobSift wordmark, clearly labeled client scope, and operator menu if supported. No logo animation or decorative header art. Search is page-scoped by default. Client changes reset incompatible filters and selected records, and announce the new scope.

Navigation, in this order: Dashboard, Jobs, Review, Search Briefs, Runs, History, Diagnostics, Settings. No empty filler sections or permanent notification counters without actionable evidence. Current navigation uses a 2px accent edge, selected surface, medium-weight text, and `aria-current`. Main begins with breadcrumb only on detail pages, one H1, compact scope/observation line, then the page's primary action. At most one filled primary button per local task region.

Common page behavior: preserve filters, sorting, page cursor, and selected record through detail navigation and Back. Encode shareable state in future routes without embedding credentials or private payloads. Every page follows the global loading/error rules in §12 and keyboard rules in §14; the table below specifies differences.

| Section | Purpose / primary question | Information | Primary / secondary actions | Default order and filters | Empty / loading / partial-error | Mobile / keyboard |
| --- | --- | --- | --- | --- | --- | --- |
| Dashboard | Triage: what needs attention now? | Attention queue, waiting matches, latest sourcing summary, source health evidence, baseline/shortfall context. | Inspect highest-priority issue; open Jobs, latest run, Diagnostics. | Failures blocking work, partial runs, review needs; then newest event. Client and explicit brief/cohort scope. | First use: “No sourcing activity recorded” with brief/plan inspection link. Load each section independently. Retain available sections and mark failed ones; never claim healthy from silence. | Single ordered column. Tab follows attention → jobs → activity → health/capacity; no custom row navigation outside lists. |
| Jobs | Compare actionable leads: which job should I inspect? | Group representative, company, role, location/mode, decision, source, freshness, outcome, times, application destination. | Inspect job; open application, inspect members/history, set outcome only when supported. | Strong then Possible, newest first seen, stable ID tie-break; Needs review via explicit filter/Review. Client, brief, decision, delivery state/destination, source, market, mode, outcome, date, text. | No data: “No jobs recorded.” Filtered empty offers Clear filters. No matching leads is distinct from collection failure. Initial text loading; refresh preserves rows. Partial coverage banner links failed targets. | Priority list → full-page detail. Focused list supports j/k, Enter; normal table links remain keyboard accessible. |
| Review | Resolve uncertainty: why does this need inspection? | Needs review queue; optional Possible and Strong queues; reasons and evidence before metadata. | Inspect next item; open application/provenance; record a supported disposition/outcome. | Needs review first, oldest first seen first, stable ID; then selectable Possible/Strong. Decision, reason category, brief, source, outcome. | “No jobs need review in this scope”; never imply all reviewed without persisted review state. Preserve selected item on refresh/error. | One job at a time with Previous/Next and queue position. j/k only in queue; explicit buttons in detail. |
| Search Briefs | Understand intent: what rules are being used? | Client identity, plan-bound brief, revision/hash, vocabulary, required/preferred/excluded/ignored dimensions, revision comparison. | Inspect brief; compare V1/V2, inspect associated plans/runs; create revision only when supported. | Bound brief first, then known revision chronology; unknown dates last. Client, revision, bound plan, rule text. | “No brief available for this client.” Load summary before comparison. If history fails, retain current rules and mark history unavailable. | Stacked rule sections; compare each field before/after instead of two wide columns. Tab disclosures; native select for revision. |
| Runs | Audit sourcing: what ran and what did it produce? | Plan/brief identity, status/time, stage counts, sources/targets, failures, suppressions and capacity evidence where reported. | Inspect run; inspect target/log evidence; execution/retry only when supported. | Newest start first, stable ID. Plan, brief, source, status, date. | “No runs recorded.” Loading status never says Running. Partial run detail preserves successful targets; failed report fetch is distinct from failed run. | Summary then target list with full target drill-down. Keyboard table links/sort controls; no automatic moving focus as progress arrives. |
| History | Recover operator memory: have we surfaced this before, and why not again? | Posting/group delivery and imported ledger evidence, Applied/Not Applied/unknown, first/last seen, identities, URL fallback, suppression reason. | Inspect historical record; open known URL, inspect workbook row/delivery event, update outcome only if supported. | Latest known event descending, unknown dates last. Client, destination, outcome, source, event type, time, URL/text. | “No history recorded.” Missing ledger access is not empty history. Retain records during refresh; distinguish incomplete imports from complete results. | Compact chronological list, labeled details; native links and Enter inspect. |
| Diagnostics | Explain engine behavior: where did the results go? | Evaluation gates, reason distribution, near misses, source health, collector failures, suppressions, dedupe members/evidence. | Drill into a count; change cohort/revision, compare equivalent cohorts, inspect raw evidence. | Pipeline order, reasons by count descending then label. Cohort/run, brief, source/target, gate, decision, suppression type. | “No diagnostic evidence for this scope.” Load counts and drill-downs independently. Missing contributing records marked unavailable; never a fabricated empty list. | Summary/distributions first; wide tables in labeled horizontal regions or full drill-down. Keyboard reaches every aggregate link and scroll region. |
| Settings | Control presentation/context: what preferences apply? | Theme, density, timezone, shortcut preferences; operator/client identity read-only where appropriate; integrations/permissions only if supported. | Save changed preferences; reset presentation defaults. | Appearance, accessibility/keyboard, context, supported connections. No filters; search unnecessary until settings volume justifies it. | Unsupported sections omitted with contextual explanation when needed. Preserve input on loading/save failure; show last saved state. | Single-column forms; standard Tab order, no character shortcuts in fields. |

## 5. Page composition and wireframes

The following diagrams describe structure, not visual mockups. Bracketed actions depend on §3 capabilities. Sample counts must be labeled as examples outside their actual evidence context.

### 5.1 Dashboard

Exact vertical anatomy: page title/scope → attention section (maximum three items, then “View all”) → waiting jobs table (five rows, linked total) → latest sourcing activity (one report) → two compact divided sections for health and capacity. On ≥1440px only health/capacity may share a row. No wall of KPI cards. If no attention is reported, one quiet sentence replaces the list; health still states coverage/time.

```text
Dashboard                         Client … · Brief … · Observed …
Attention
  Run completed with 2 source failures                     Inspect
  Jobs with unresolved market evidence                    Review
─────────────────────────────────────────────────────────────────
Waiting jobs                                      View all jobs
Role                    Company       Match             First seen
…             …                       Strong match      …
─────────────────────────────────────────────────────────────────
Latest sourcing activity                        Open run
Status + explanatory sentence · plan · started/completed
Targets …   Received postings …   Delivered groups … (if reported)
─────────────────────────────────────────────────────────────────
Source health                          Capacity baseline
Observed targets / failures / time     V2 equivalent: 56 fresh groups
Inspect diagnostics                    Frozen cohort · inspect basis
```

### 5.2 Jobs and selected detail

Use a table at ≥1024px; rows default to 40px minimum, compact preference 32px minimum, comfortable 48px. Compact is for fine-pointer desktop only. Text zoom or wrapped content increases row height; never clip vertically to hit a density target. At 1440×900, target at least 16 default rows or 20 compact rows after shell/toolbars; 20–40 records should take roughly one to two viewport heights. Accessibility wins over that target.

| Column | Priority / width guidance | Rules |
| --- | --- | --- |
| Role | Primary, flexible, minimum 240px | Weight 500; first in DOM and reading order. One line in dense table; full text in accessible detail and on focus disclosure. |
| Company | Primary, 160px | Plain text, no mandatory logo. Sort alphabetically. |
| Location / work mode | Secondary, 180px | Location and explicit mode as text; unknown stays unknown. Full restriction in detail. |
| Match decision | Primary, 120px | Text label; no probability invented from ranking score. |
| Source | Secondary, 104px | Representative source; “+2 postings” opens group members, not implied source count. |
| Freshness | Secondary, 112px | Only backend-backed New/Seen/delivery evidence; otherwise first-seen date. Never equate freshness with live availability. |
| Operator outcome | Secondary, 112px | Applied, Not applied, Unknown; blank is not Not applied. |
| First seen | Optional default on wide screens, 128px | Exact date/time available; sortable. |
| Application destination | Detail by default, optional 160px | Domain plus labeled Open application action; distinguish canonical listing fallback. |

Widths are minima, not permission to squeeze unreadable text. When the default columns do not fit, move First seen, Source, Freshness, Operator outcome, then Location/work mode into detail in that order; retain Role, Company, and Match. The column chooser must report fields moved into detail. With detail open, keep Role, Company, Match in the table and move secondary fields into detail; preserve optional columns in a column chooser. Do not horizontally scroll the entire page.

Hover changes background only; no shifting columns, hidden essential buttons, or raised rows. Keyboard active row has a focus outline; inspected row has selected fill and a 2px leading accent edge. Bulk checkbox selection is a separate state with visible checkboxes and selected count. Do not conflate keyboard position, detail selection, and mutation selection.

Detail is a nonmodal 400px panel (maximum 480px) when at least 560px remains for the table. Otherwise use a routed full-page detail with Back preserving position. On large screens table and panel can scroll independently with labeled regions; keyboard focus must remain visible. Opening via Enter moves focus to the detail heading; closing restores the originating link. Do not trap focus in a nonmodal pane.

Sorting is single-column by default, with visible direction and deterministic ID tie-break. Unknown values sort last. Text search covers title/company/location, explicitly labeled “Search these jobs”; full-description search requires a supported query contract. Submit with Enter; do not flash results on every keystroke. Filters show active values, Clear filters, result unit and scope. Never sort only the loaded page while implying global order.

Use cursor pagination, 40 records per page, optional 20/80; keep a stable snapshot if backend supports it. Show Previous/Next and total only when known; otherwise “40 groups shown.” No infinite scroll. Incoming data produces a “New results available” control; never moves the selected row automatically.

Bulk mutations are off by default. Enable only a supported, reversible operation with explicit record scope, selected count, per-record result, and partial-failure recovery. Selection defaults to this page and clears on incompatible scope changes. Never bulk-apply to unseen filtered records without a separately explicit selection. Opening multiple application tabs is not a bulk action.

```text
Jobs · groups        Client … / Brief … / Destination …
Search these jobs [        ]  Filters  Density  Columns
────────────────────────────────┬──────────────────────────────
Role       Company    Match     │ Role · Company        Close
…          …          Strong match │ Location · mode
> …        …          Possible  │ Possible — explanation
…          …          Needs review │ Why it matched / needs review
                                │ Evidence rows …
Previous   40 groups shown  Next│ Open application · domain
                                │ Outcome: Unknown
                                │ ▸ Provenance  ▸ Group members
```

### 5.3 Review

A decision workspace, not a duplicate jobs catalogue. Queue occupies 280px on wide desktop; remaining space prioritizes reasons, evidence, then description/application destination. Default Needs review queue; Possible and Strong are explicit secondary filters, never renamed as pending-review truth. “Inspected this session” may be local navigation state but cannot be persisted or displayed as Reviewed without a supported ledger. Do not auto-advance on link opening. After a confirmed supported disposition, Next is available; failed saves retain item and input.

```text
Review · Needs review [queue filter] · Brief …
──────────────────┬───────────────────────────────────────────
Oldest first      │ Role · Company                 Item … of …
> Role / company  │ Needs review: market evidence is unknown
  Role / company  │ Why it needs review
  Role / company  │ Requirement | Observed evidence | Result
                  │ Why it matched (separate supporting reasons)
                  │ ▸ Source evidence  ▸ Full description
                  │ Open application
                  │ Outcome / disposition [only if supported]
                  │ Previous                              Next
```

### 5.4 Search Brief detail / revision

Identity block above rules: Client display name + stable ID; separately “SearchBrief V2” + schema version + file/hash; separately plan binding. Rules use rows with Dimension, Intent, Values, Unknown handling. Vocabulary is a wrapping text list, not dozens of pills. Show market/work mode first, role vocabulary next, then preferences/exclusions. Ignored dimensions have their own visible summary, with expandable detail; they are not hidden defaults.

V1/V2 comparison is field-aligned before/after with Added/Removed/Unchanged text, not color-only diffs. Show changed values and unchanged constraint summary. Display exact artifact hashes; do not fabricate revision author/date. V2 adds role vocabulary while retaining the schema identifier; compare actual files, not label assumptions. Legacy compatibility translations must name their origin; never relabel former work-eligibility constraints as target market.

```text
Search Briefs / SearchBrief V2                 Compare V1 / V2
Client: … · Stable client ID: …
Revision: V2 · Schema: operator-style-sourcing-brief-v1
Used by plan: …                      Artifact / hash ▸
──────────────────────────────────────────────────────────────
Dimension       Intent       Values                Unknown policy
Target market   Required     United States         Review
Work mode       Required     Remote                Review
Role vocabulary              IT Support Specialist, …
Preferred terms              Networking, Linux, …
Exclusions                   Management; excluded titles …
Ignored dimensions           Employment type; work eligibility …
Residence                    Lagos, Nigeria · informational
──────────────────────────────────────────────────────────────
Revision history       V1 … / V2 …         Inspect / Compare
[Create revision, only after persistence contract exists]
```

### 5.5 Run detail

Header status and explanatory text are inseparable: “Partial — 2 targets failed; results from 8 targets retained.” Follow with plan/brief snapshot, exact times, pipeline accounting, target outcomes, failures, then suppression/capacity/control detail. A successful run may have zero eligible jobs; it is not failed. Partial is an execution fact, not an estimated fraction complete.

Acquired means received postings if that is the report field; normalized means a separately reported normalization count, never silently reused received count. Match counts name postings; exported counts retain their recorded unit until a group-level delivery report establishes groups. Stage counts need not form a funnel: overlapping reasons and groups use separate denominators. Requests show used/limit only if both reported. Cost shows currency, observation time, and estimated/actual basis; absent cost is “Not reported,” not “Free.” Retry/pause/cancel controls must be absent without backend support.

```text
Runs / run identifier        Partial — … targets failed
Plan …   SearchBrief revision …   Observed / snapshot …
Started …   Completed …   Duration … (from known timestamps)
──────────────────────────────────────────────────────────────
Stage                     Count / unit               Inspect
Acquired                  … postings                  …
Normalized                Not reported
Matched                   … postings                  …
Fresh deliveries          … [reported unit]           …
Suppressions              … / Not reported            …
──────────────────────────────────────────────────────────────
Target                 Source      Status + explanation
…                      …           Success — … received
…                      …           Failed — timeout
──────────────────────────────────────────────────────────────
Failures and retained partial results
Request/cost controls · Shortfall basis · Provenance ▸
```

### 5.6 History

Keep original imported ledger records even when there is no corresponding current posting. Link related delivery/group events without inventing a combined outcome. Conflicting records show their individual values, origin, and time; do not pick a winner without backend precedence. URL fallback evidence must identify conservative normalized URL matching versus exact source identity. Blacklist-sheet provenance is not an active blacklist policy.

```text
History · Client … · Destination …        Search URL / company
Outcome [All]   Event [All]   Source [All]   Date […]
──────────────────────────────────────────────────────────────
Record / role     Event             Outcome       Last known event
…                 Imported         Not applied   … / Not recorded
…                 Delivered        Unknown       …
──────────────────────────────────────────────────────────────
Selected record
First seen … / Last seen … (posting observations, if available)
Suppressed because: Previously surfaced in imported history
Matched by: exact source identity / normalized URL fallback
Original URL · source/target/provider ID · delivery destination
Import workbook checksum / sheet / row · related records ▸
```

### 5.7 Diagnostics

Use count tables and compact horizontal bars, not decorative charts. Gates follow pipeline order; downstream reasons sort by descending count. Every bar includes exact count and denominator; scale starts at zero, shared across comparable rows. If a posting has multiple reasons, label “Reason occurrences; postings may contribute more than once”; do not force percentages to total 100%.

The V1 investigation reference of 48,710 evaluated, 48,441 title-gate rejects, and 269 passes is a requested historical illustration: show it as measured only when its exact frozen report/cohort is available. It is not a live default or a V2 funnel. The three numbers reconcile, but reconciliation alone does not establish provenance. Downstream distribution and near-miss titles must come from records, not fabricated illustrative reasons. Near miss is diagnostic language, never a new match category or a suggestion to loosen rules automatically.

Every count should link to contributing records with identical cohort/filter scope. Where record-level evidence is unavailable, render the count as plain text and say “Contributing records unavailable”; do not offer a dead drill-down. Dedupe inspection shows representative, all members, evidence kind/version, preserved source identities, and suppression effects. Source health names last attempted time, outcome, coverage, and failure reason; no opaque health score.

```text
Diagnostics · Frozen cohort … · Brief V1 … · Evidence timestamp …
Pipeline / Reasons / Sources / Suppression / Dedupe  (local tabs)
──────────────────────────────────────────────────────────────
Gate                     Postings           Inspect
Evaluated                48,710             [records if available]
Title gate rejected      48,441             …
Title gate passed           269             …
──────────────────────────────────────────────────────────────
Downstream rejection distribution
Reason                   Count    Compact bar / denominator
…                        …        …
Near-miss titles          Evidence / exact decision / inspect
──────────────────────────────────────────────────────────────
Source health / collector failures
Suppression by cause and accounting window
Duplicate groups / members / evidence / representative
```

## 6. Visual tokens

Token names below are the shared vocabulary; future code must centralize them. No arbitrary per-page colors, radii, spacing, or typography. Values are CSS pixels unless stated otherwise; use rem equivalents for text and scalable dimensions.

### Typography

Primary: `system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`. Optional technical text: `ui-monospace, "SFMono-Regular", Consolas, monospace`. No additional display font. Platform glyph differences are accepted; hierarchy/metrics are fixed.

| Token | Size / line height / weight | Use |
| --- | --- | --- |
| type.metadata | 12 / 16 / 400 | Secondary timestamps, provenance; never the only action label. |
| type.compact | 13 / 18 / 400 | Compact desktop table text. |
| type.body | 14 / 20 / 400 | Default UI, forms, table rows. |
| type.label | 14 / 20 / 500 | Labels, buttons, table headers. |
| type.section | 16 / 24 / 600 | H2 sections. |
| type.subsection | 14 / 20 / 600 | H3 under an H2. |
| type.title | 24 / 32 / 600 | Sole page H1; 20 / 28 on mobile. |
| type.reading | 16 / 24 / 400 | Long job descriptions and mobile inputs. |

Use weights 400/500/600 only, normal letter spacing, sentence case. No all-caps metadata. Numbers use tabular lining figures; count columns right-align, textual columns left-align. Use localized thousands separators; never abbreviate audit counts as “48.7k.” Technical IDs wrap or have accessible copy controls; monospace is not a general aesthetic. Metadata retains contrast, not reduced opacity. Dates expose exact timestamp/timezone inline in detail; relative age is supplemental. Default timezone is user's system timezone, visibly named; a preference may override it. Missing timestamps say “Not recorded.”

### Color

Light and dark are equally supported. Default follows system; saved explicit preference overrides it. No translucent surfaces. Hex pairs below are light / dark.

| Token | Light | Dark | Use |
| --- | --- | --- | --- |
| surface.canvas | #F7F8FA | #111318 | Page background |
| surface.base | #FFFFFF | #181B22 | Main work surface |
| surface.subtle | #EEF1F5 | #222630 | Headers, quiet grouping |
| surface.hover | #E8EDF3 | #2B303B | Hover only |
| surface.selected | #E8F0FD | #233752 | Selection fill |
| text.primary | #20242C | #F2F4F7 | Main text |
| text.secondary | #4B5563 | #B8C0CC | Supporting text |
| text.muted | #606B7A | #A0ABBA | Metadata |
| border.subtle | #D6DBE3 | #353D49 | Nonessential dividers |
| border.strong | #7A8594 | #778396 | Input/control boundary |
| accent.text / focus | #2459A6 | #92B9F5 | Links, selection edge, focus |
| accent.solid | #2459A6 | #92B9F5 | Primary button fill |
| accent.on-solid | #FFFFFF | #111318 | Primary button text |
| success.text | #24633E | #8DCAA4 | Confirmed successful execution |
| success.surface | #EAF3ED | #183324 | Restrained exceptional status background |
| warning.text | #785000 | #E5BF70 | Partial, needs attention |
| warning.surface | #FFF3D9 | #382D16 | Warning background |
| error.text | #A12D32 | #F1A1A6 | Failed operations |
| error.surface | #FCEDEF | #3B2228 | Error background |
| info.text | #2459A6 | #92B9F5 | Informational notice |
| info.surface | #E8F0FD | #233752 | Notice background |

Only use semantic foreground on its matching semantic surface or base/canvas/subtle. Primary/secondary/muted text may appear on neutral surfaces, including selected/hover. Links are underlined in prose; table links have a persistent recognizable affordance, not hover-only discoverability. Primary button hover uses a 2px inset border of on-solid color; no unlisted shades. Disabled controls use secondary text/subtle surface plus explicit disabled semantics, not opacity that destroys contrast.

Normal text must meet 4.5:1; large text 3:1; meaningful controls, icons, and focus indicators 3:1 against adjacent colors. `border.subtle` is decorative and must not be the only control boundary. Use strong borders for inputs. Neutral badges use text.secondary on surface.subtle; warning badges use warning.text on warning.surface. Semantic text inside hovered/selected rows retains its own matching semantic surface when needed for the permitted pairing. The focus ring is 2px solid focus, 2px offset with base-colored separation; verify against the actual surroundings. Text plus edge/focus semantics make selection discernible independent of fill contrast. Verify all rendered token combinations before release, including hover, error, selected, and disabled explanatory text; tokens alone do not certify an implementation.

### Spacing and geometry

Base unit 4px; full permitted spacing scale: 0, 2, 4, 8, 12, 16, 20, 24, 32, 40, 48, 64. The 2px half-step is for icon gaps/alignment only. Main gutters: 32 large desktop, 24 laptop, 20 tablet, 16 mobile. Title-to-toolbar 16; toolbar-to-content 16; related sections 24; major sections 32. Inline label/value gap 8; form field gap 20; label-to-control 8; help/error gap 4; form sections 32. Table cells: 12 horizontal, 8 vertical default; compact 8 horizontal/4 vertical with compact typography; comfortable 12 horizontal/12 vertical. Row heights are minima.

Radius tokens: 0 tables/sections; 2 status labels; 4 inputs/buttons; 6 popovers; 8 modal/panel corners where floating. No pill default. Border width 1px; active edge/focus 2px. Dividers separate semantic groups, never every nested block. Cards, when justified, use 1px subtle border and radius 6, no shadow.

Shadow policy: only elevated transient surfaces, `0 4px 16px rgba(16,24,40,0.12)` light and `0 4px 16px rgba(0,0,0,0.32)` dark; always add a border. No shadow on headers, buttons, tables, selected rows, or permanent panels. Modal backdrop is the sole permitted translucent layer: black at 40%; no blur.

Controls: 32px minimum desktop height, 40px comfortable, 44px touch. Search/inputs 36px desktop, 44px touch. Text-oriented settings/forms max width 720px; descriptions max 72ch; operational tables use available width.

### Icons and motion

Icons only for recognizable actions (search, close, external link, disclosure, sort) or useful status reinforcement. Use one consistent outline family when implementation selects one, 16px default/20px touch, 1.5px stroke; 24px minimum desktop hit area, 44px touch. Decorative SVGs are hidden from assistive technology; icon-only buttons have explicit names. Do not precede each nav label or field with an icon.

Motion tokens: immediate 0ms; feedback 100ms; transient enter/exit 160ms; maximum 200ms. Easing `cubic-bezier(0.2, 0, 0, 1)` for entrance and standard ease-out for feedback. Permit background/opacity transitions and at most 4px transient displacement. Reduced motion removes displacement and uses 0ms transitions; loading is static text. Never animate numbers, table insertion/reordering, status colors, chart bars, focus position, page skeletons, or background gradients. No spring/bounce effects; no perpetual pulse. Status updates never move the current target.

## 7. Component rules

**Cards are not the default container.** Try whitespace, a heading, a divider, or shared table columns first.

| Component | Use | Misuse to reject |
| --- | --- | --- |
| Table | Comparing repeated records with shared fields; semantic headers/caption. | Layout scaffolding; unlabeled cells; squeezing every field into view. |
| List | Chronological events, compact mobile jobs, short reason sets. | Misaligned pseudo-table with comparable numbers. |
| Card | A genuinely independent selectable object with its own lifecycle, e.g. a saved plan summary among few plans. | One card per metric, form field, reason, or job. |
| Panel | Persistent contextual detail beside a work list; usually divider-separated. | Nested boxes, duplicated navigation, essential evidence hidden by default. |
| Drawer | Temporary secondary task on constrained widths; modal semantics if it blocks the page. | Long primary job reading on mobile; use routed detail instead. |
| Modal | Bounded confirmation or short blocking choice; maximum 560px, 640px for complex confirmation. | Full briefs, diagnostics, routine navigation, or stacked modals. |
| Popover | Compact filter/column chooser; maximum 320px and viewport-bound. | Long forms or essential evidence available nowhere else. |
| Tooltip | Supplemental shortcut or expansion, shown on hover and focus, dismissible. | Only source of a label, error, reason, or actionable content. |
| Tabs | Peer views of one record/workspace, e.g. Diagnostics dimensions. | Primary app navigation, sequential forms, hidden errors. |
| Segmented control | Two or three mutually exclusive compact choices, e.g. density. | Numerous filters, multi-select categories, unexplained icons. |
| Badge | One compact categorical decision in a scan-heavy table/header. | Every metadata value or several competing states in one cell. |
| Inline status | Default status presentation: label + consequence/detail. | Color dot alone or vague “Issue.” |
| Banner | Persistent scope-wide partial/stale/error state with recovery link. | Routine saved feedback or multiple stacked promotions. |
| Toast | Noncritical completion acknowledgement; 5 seconds, pause while hovered/focused. | Sole error notice, critical evidence, or expiring undo required for recovery. |
| Command menu | Future searchable navigation and safe actions; standard visible alternatives remain. | Hidden-only workflows, immediate destructive execution. |

Primary buttons use solid accent; secondary buttons base surface/strong border; tertiary actions text with clear hit area. Destructive confirmation uses explicit action wording and error treatment only at the final action, never as default page emphasis. Menus require keyboard dismissal and focus restoration; tooltips cannot contain controls.

## 8. Status language

Keep four independent dimensions: matcher decision, delivery/history, operator outcome, and execution/data quality. Never compress them into a single “job status.” Badge maximum: one decision badge per job row; other dimensions use plain text in their own columns. Badges use 12/16 weight 500, 4px horizontal padding, minimum 20px height, radius 2; badge itself is not clickable. Labels have screen-reader context such as “Match decision: Possible.”

| Label | Treatment | Meaning / accompanying explanation |
| --- | --- | --- |
| Strong match | Neutral bold text, optional neutral badge | Matcher category; no green approval check. Reasons required in detail. |
| Possible | Neutral text, optional neutral badge | Possible match; not a percentage or prediction. |
| Needs review | Warning text, optional warning badge | Name missing/conflicting evidence; never deliver as eligible by presentation alone. |
| Rejected | Secondary text | Matcher rejection, not system error; link exact rejection reasons. |
| New / Seen | Plain secondary text | Only with authoritative meaning/scope; first-seen time is safer than fabricated viewing history. |
| Applied / Not applied | Plain primary/secondary text | Recorded operator outcome with origin; no success/failure judgment. Normalize display case only. |
| Historical | Plain metadata | Evidence comes from history, not a current vacancy claim. |
| Already delivered | Plain secondary text | Include date if known and destination in detail. |
| Success | Success text; optional check icon | Execution completed successfully; explain coverage/results. |
| Partial | Warning text; optional warning icon | Retained results plus named missing/failed coverage. |
| Failed | Error text; optional error icon | Execution failed; cause, retained evidence, available recovery. Backend `failure` maps here. |
| Running | Info text, static activity icon optional | Only authoritative in-progress state; counts “so far,” unknown ETA stays unknown. |
| Paused | Neutral text, pause icon optional | Requires authoritative pause state, time/reason/resume capability. |
| Canceled | Neutral text | Confirmed cancellation; identify retained partial work. Not synonymous with Failed. |
| Unknown | Plain secondary text | Fact unknown; no blank dash or red badge. |
| Stale | Warning inline text | Timestamp plus backend freshness policy or explicit failed refresh; no invented expiry threshold. |
| Unavailable | Plain text or error notice according to cause | Fact could not be retrieved; distinguish absence from zero. |

Icons are optional reinforcement, at most one per status, hidden from screen readers when text repeats them. Do not use rainbow badge combinations, blinking state, celebratory success, or generic green/red treatment. Screen readers receive textual status and important changes via polite live region; urgent blocking action failure may use an alert once. Never announce every changing count.

## 9. Match explanations and provenance

Detail order: decision + one-sentence reason summary → **Why it matched** → **Why it needs review** or **Why it was rejected**, when applicable → **Why it wasn't delivered**, if suppression/non-delivery evidence exists. Supporting and blocking evidence remain separate, even when both exist. No inferred rationale, synonym replacement that changes meaning, or fabricated confidence.

Use a three-column evidence pattern: Rule / Observed evidence / Result. Plain-language summaries map deterministically to stored reasons; preserve original reason text in expandable evidence. Quotes must be exact excerpts with source field. If the backend reason has no excerpt/location, say “Evidence excerpt not recorded.”

Example structure, only populated when backed by the selected record:

```text
Why it matched
Role             Technical Support Specialist     Title matched
Work mode        Remote                           Required rule met
Target market    United States                    Required rule met
Preferred term   Networking                       Preference matched

Why it wasn't delivered
Suppressed because: Previously delivered Sep 8 [year/time in detail]
Destination: …     Inspect delivery record
```

Non-delivery is not necessarily rejection. Distinguish historical identity match, normalized URL history fallback, previously delivered group, nonrepresentative duplicate member, and ineligible decision. If the backend reports only non-delivery without cause, say “Delivery reason not reported.” Do not label every absent export “Suppressed.”

Quiet provenance disclosure sits below reasons, one click/key action away. It exposes source, exact source target coordinates, provider/posting/group IDs, first/last seen, source publication/update times separately, original/canonical/application URLs, brief revision/schema/hash, run/cohort, matcher evidence, historical state and destination. Label each URL's role; application action prefers the backend-selected direct URL, otherwise “Open listing.” If neither exists, show “Application URL unavailable.” Do not construct or guess URLs. Preserve alternatives and group members.

Human-readable metadata comes first, raw payload last. Technical identifiers may be copied with feedback; secrets must not enter UI logs or URLs. Reason/provenance headings stay in normal reading order and are searchable within detail.

## 10. Forms and revision editing

Future brief editing uses sections: Role vocabulary → Market and work mode → Preferred terms and exclusions → Advanced restrictions. Each section has a concise read summary; advanced restrictions collapsed until requested, but their active/ignored status is always summarized. No giant undifferentiated form.

Labels above controls; never placeholder-only. Required/optional state is explicit. Descriptions explain consequences, not field implementation. Country/role lists allow reviewing and removing each value without drag-only interaction. Intent choices show their meaning; Unknown handling remains a separate choice. Required terms and preferred terms cannot share an unlabeled field.

Validate after field blur and on save, without shouting errors while typing. Show inline error plus linked error summary at top; focus summary on failed submission. Preserve all entered values. Server rejection remains visible until resolved. No automatic clearing or success before acknowledgement.

Use “Create revision” for changing sourcing rules, with a before/after summary and explicit unchanged client ID. Never silently mutate a previously used revision. Saving and activating are separate effects unless an explicit backend atomic operation provides both and the button names both. On success show the returned revision and binding; on ambiguous network outcome check persisted state before offering retry. Concurrent revision conflict retains draft and offers compare/reload; never silently overwrite.

Changing Required to Preferred/Ignored, removing exclusions, expanding role/market/mode coverage, or relaxing unknown handling gets a factual broadening warning naming the changed rules. Do not predict extra jobs without a measured evaluation. Confirmation reviews the concrete diff; it does not claim safety or capacity. Keep candidate residence separate from work eligibility. Notes must state whether they influence matching; do not imply free text changes rules.

Unsaved state: “Unsaved changes” next to Save/Create revision; navigation guard offers Keep editing or Discard changes. Do not autosave business rules. Appearance settings may save immediately with visible acknowledgement and failure recovery. Destructive operations require named scope, consequence, and confirmation; unsupported delete/reset-history actions are absent. Failure retains prior authoritative state and offers an idempotent recovery path only where supported.

## 11. Responsive composition

| Range | Behavior |
| --- | --- |
| Large desktop ≥1440px | 200px navigation; 32px gutters; table and 400–480px detail pane; optional secondary columns. Main work area may grow; reading text stays bounded. |
| Laptop 1024–1439px | 200px navigation, 24px gutters; priority columns. Pane only if table retains ≥560px, otherwise routed detail. |
| Tablet 768–1023px | Navigation behind labeled Menu; 20px gutters; touch targets; single work surface. Brief comparisons become stacked field diffs. |
| Mobile <768px | 16px gutters; 44px controls; full-page job detail and compact lists; filters in accessible dialog/drawer. No horizontal page scrolling. |

Mobile priorities: check run/health status, inspect one job's reasons, open application, record supported outcome. Jobs list shows role, company, location/mode, decision, and outcome; source/times remain in detail. One divider per item, no individual cards. Reflow order must match DOM order. At 320 CSS px, text wraps and controls stack. Tables truly requiring two-dimensional comparison (Diagnostics) may scroll inside a labeled, keyboard-focusable region; offer row-detail/list access to the same essential data. Do not remove truth or actions solely because a column is hidden. No hover dependency or drag-only resizing. Portrait and landscape both work.

## 12. Empty, loading, refresh, and failure states

| State | Required behavior / example |
| --- | --- |
| First use | Explain absent evidence and first supported action: “No runs recorded. Inspect a sourcing plan.” No fake data, illustrations, or congratulations. |
| Filtered empty | “No groups match these filters.” Keep filters visible; Clear filters action. |
| No matches | “No fresh jobs met this brief” only with complete supporting evaluation/delivery evidence. Link reasons/suppressions. Partial acquisition instead says “No fresh jobs in the available results; coverage is partial.” |
| Initial loading | Inline “Loading jobs…” with busy semantics. Use a stable reserved content area; skeleton only for predictable layout with a material wait, never artificial rows/counts or shimmer. |
| Background refresh | Keep previous usable records, selection, and scroll. Show “Refreshing…” with observation timestamp; do not blank the page. |
| Stale data | “Showing results from [time]; refresh failed” or a backend stale reason. Retry stays available; never imply records are currently live. |
| Partial data | Name missing coverage, retained results, and consequence; link run/target diagnostics. Counts explicitly limited to available evidence. |
| Source failure | “Workday target … failed: timeout.” Preserve successful targets and historical jobs. No inferred closures. |
| Permission failure | “You do not have access to this run.” Keep unaffected authorized content; offer supported account/context recovery. Do not leak inaccessible record metadata. |
| Network failure | Distinguish fetch failure from engine failure. “Could not load this run. Retry.” If cached data exists, label its time. |
| Destructive operation failure | Persistent inline failure, preserved original state, concrete result per item if partial. No success toast or blind duplicate submission. |

Show loading feedback if unresolved after 200ms; never delay a fast result to display it. Reserve banners for scope-wide facts, inline messages for local failures. Retry user actions only according to backend idempotency. No endless spinner without explanatory text/recovery. A failed save is not dismissed by a transient toast. Where data sources disagree, show the conflict and origin rather than smoothing it away.

## 13. Accessibility contract

Minimum: [WCAG 2.2 AA](https://www.w3.org/TR/WCAG22/) across full pages and complete workflows. The following are JobSift implementation requirements, including stronger defaults where useful:

- Semantic landmarks, skip link, one H1, logical headings; native buttons, links, labels, tables and form controls first.
- Complete keyboard operation, visible unobscured focus, predictable focus return, no traps except correctly managed modal focus.
- Normal text contrast ≥4.5:1, large text ≥3:1, meaningful non-text UI ≥3:1; text and icons supplement all semantic colors.
- Minimum pointer target 24×24px desktop; 44×44px touch. Dense rows may be 32px only with adequately separated targets. Never reduce hit targets to icon bounds.
- Accessible names include visible labels. Errors associate with fields; submission error summary links to each error and retains entered data.
- Tables use captions, scoped headers, named sort buttons with `aria-sort`, explicit checkbox labels. Avoid `role=grid` unless its complete interaction model is implemented. Visual selection must have an accessible equivalent, not inappropriate `aria-selected` on a plain table row.
- Test at 200% text resize and 400% zoom, including 320 CSS px reflow and text-spacing overrides; no lost actions or overlapping text.
- Reduced-motion behavior in §6; no timed-only recovery, hover-only controls, inaccessible tooltips, drag-only interactions, or forced orientation.
- Dialogs have names, initial focus, contained keyboard navigation, Escape where safe, and focus return. Nonmodal panes remain in document order.
- Announce asynchronous results concisely with status regions; assertive alerts only for blocking action failures. Virtualization must not erase essential table semantics; default pagination avoids that dependency.
- If authentication is later introduced, support password managers/paste and accessible authentication; no memory puzzle required to use the workbench.

Automated checks are necessary but insufficient. Release requires manual keyboard, screen-reader (at least VoiceOver/Safari and NVDA/Firefox or equivalent supported combinations), zoom, touch, and both-theme checks on representative complete workflows.

## 14. Keyboard and power-user conventions

| Key | Behavior |
| --- | --- |
| `/` | Focus current page search when present; no-op elsewhere. |
| `j` / `k` | Next/previous row only while the relevant list/table region is focused; no wrap at ends, no mutation or application opening. |
| Enter | Activate focused link/control; on a focused row-inspect affordance, open selected detail. Never override native form behavior. |
| Esc | Close innermost transient surface; then detail if appropriate. Restore focus. If an unsaved edit would be lost, show the navigation guard. |
| Cmd/Ctrl+K | Future command menu when implemented and available; provides navigation first, explicitly confirmed supported actions second. |
| Tab / Shift+Tab | Normal document traversal; all custom shortcuts have visible alternatives. |

Character shortcuts must be disableable in Settings and documented in a visible Keyboard shortcuts control. Ignore them in inputs, textareas, editable content, composing text, and active dialogs except their own documented controls. Do not intercept browser/system shortcuts (Find, reload, tab/window controls, Back, Save). Register Cmd/Ctrl+K only for the documented menu and allow it to be disabled. No global j/k or undiscoverable keyboard-only feature. Queue movement updates a concise accessible position description; it never reads the whole record on every move. Arrow-key behavior stays native unless a standard widget pattern requires otherwise.

## 15. Copy standard

Concise, calm, factual, precise, non-celebratory, and non-anthropomorphic. Use sentence case, concrete nouns and explicit verbs. No exclamation points for routine success. Button labels name actions: Inspect run, Open application, Compare revisions, Create revision, Retry. Avoid “Submit,” “Proceed,” or ambiguous “Done” where a specific effect matters.

Prefer “12 new matches,” “Run completed with 2 source failures,” “No fresh jobs met this brief,” and “56 fresh groups in the current baseline,” always with the scope qualifications required above. Prohibit “Great news!”, “AI magic,” “We found amazing opportunities!”, “Oops!”, and “Sit tight.” JobSift evaluates evidence; it does not think, love a role, or guarantee fit.

Use “Not applied” in UI and preserve imported `Not Applied` as raw provenance. “Rejected” always means matcher rejection unless explicitly attributed otherwise. Explain title gate as “Role title filter” in operator summaries, retaining “title gate” in technical diagnostics. Do not hide useful technical names such as source target or SearchBrief revision behind vague copy.

## 16. Reviewer rejection checklist

Reject a proposed interface if any item applies:

- [ ] A page could be swapped with a generic SaaS dashboard without changing its structure or terminology.
- [ ] KPI cards, oversized typography, rounded containers, gradients, shadows, or ornamental icons dominate the records.
- [ ] Density falls below §5 targets at reference size without an accessibility/content reason.
- [ ] Alignment, type, spacing, colors, or component semantics deviate from tokens without an approved contract amendment.
- [ ] Color alone distinguishes match, execution, history, or outcome; more than one decision badge competes in a row.
- [ ] Reasons require raw JSON or multiple navigations; provenance is absent or overwhelms the primary scan.
- [ ] A count lacks unit/cohort/time/completeness, or baseline 56 is presented as actual live deliveries.
- [ ] New, Seen, Applied, historical suppression, and delivered are conflated.
- [ ] A brief revision changes client identity, schema V1 is mislabeled V2, or active revision is guessed.
- [ ] Partial/failure/unknown/stale states disappear, become zero, or are replaced by optimistic success.
- [ ] Unsupported run controls, outcome writes, revisions, or bulk actions appear functional.
- [ ] Refresh reorders work under the pointer, resets selection, or erases usable data.
- [ ] Keyboard, focus, screen-reader, zoom, reduced-motion, or touch access is incomplete.
- [ ] Mobile hides critical evidence or uses a horizontally scrolling page.
- [ ] Novelty makes screenshots attractive while adding steps to inspect a job or explain a run.

## 17. Implementation acceptance criteria

A future UI change is accepted only with review evidence for the relevant rows below. These criteria do not authorize implementation now.

| Area | Observable acceptance evidence |
| --- | --- |
| Shared language | Two independently implemented pages use the same tokens, control sizes, headers, status labels, and spacing. Exceptions are documented here before adoption. |
| Job scanning | At 1440×900, verify ≥16 default or ≥20 compact visible rows with ordinary single-line data; show 40 records in about two compact viewports. Long content/zoom remains accessible without fixed-height clipping. |
| Job inspection | From a focused row, Enter exposes decision/reasons and application destination; Esc/Back restores position. Pointer and screen-reader flows achieve the same result. |
| Evidence fidelity | Fixtures cover all four decisions, unknown market/mode, missing URLs, alternate postings, historical Applied/Not Applied/blank, prior delivery, and distinct suppression causes. UI introduces no decisions of its own. |
| Brief identity | V1/V2 comparison shows unchanged client and schema plus exact artifact identity, added/removed vocabulary, ignored rules, and unknown handling. Missing active pointer is explicit. |
| Capacity | Baseline 56 is tied to the frozen equivalent cohort; actual exports and eligible posting counts are separate. 200/250 target gaps are labeled baseline comparisons. |
| Run truth | Success with zero matches, Partial with usable records, Failed, unavailable report, and unreported metrics render distinctly. Future lifecycle labels tested only once authoritative states exist. |
| Diagnostics | Gate reconciliation and denominator definitions are visible; a count drill-down preserves exact scope, or explicitly states unavailable record evidence. No overlapping reasons presented as disjoint. |
| Persistence | Future save/outcome flows prove acknowledged success, validation failure, ambiguous timeout, conflict, and partial bulk failure. Unsupported capabilities have no misleading action. |
| State resilience | First-use, filtered empty, no matches, slow load, stale refresh, source failure, permission and network failure all preserve context appropriately. |
| Accessibility | Contrast checks for every rendered pairing; keyboard/screen-reader/zoom/touch/reduced-motion checks pass the relevant §13 flows. |
| Responsive | 320, 768, 1024, and 1440 CSS px checked; no page overflow, no missing primary action, no focus hidden by shell/pane. |
| Visual durability | Review in both themes without images or animation: hierarchy, typography, alignment and state clarity must carry the design. |

## 18. Principal design review and remaining dependencies

Review conclusion: this contract deliberately anchors appearance in durable typography, compact aligned rows, bounded reading widths, neutral surfaces, and one accent. Its structure prioritizes repeated operator work over promotional composition. Shared concrete tokens and page anatomy constrain engineers to one visual language; explicit evidence gates preserve backend truth.

The 2031 test is satisfied by avoiding decorative trends and framework dependency. The workflow test is satisfied by direct reasons, stable pagination, recoverable detail navigation, and actionable partial-state explanations. The consistency test is objective through dimensions, tokens, status mapping, and acceptance evidence. These are design conclusions, not claims that a frontend has been implemented or usability-tested.

Document validation: all 70 checked light/dark text and control-border pairings meet their specified contrast threshold (minimum checked text ratio 4.59:1); local reference links resolve and code fences are balanced. The full document was reviewed for truth boundaries, density, responsive composition, accessibility, and long-term consistency. Rendered UI validation remains a future implementation requirement.

No visual decision remains open. Backend dependencies remain: persisted revision identity/activation; authenticated operator/permissions; execution lifecycle and controls; outcome/disposition mutation semantics; stable query pagination and group-aware endpoints; freshness policy; normalization/suppression/request/cost telemetry; and record-level diagnostic provenance for historical investigations. Their exact API/storage design belongs to later backend/product work. Until then use the read-only/unavailable behavior specified here, never browser-invented truth.
