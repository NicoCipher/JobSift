# SmartRecruiters public provider contract

Verified against official documentation on 2026-10-01:

- [Public Posting API endpoints](https://developers.smartrecruiters.com/docs/endpoints)
- [Active posting index reference](https://developers.smartrecruiters.com/reference/v1listpostings)
- [Posting and location objects](https://developers.smartrecruiters.com/docs/objects)
- [Paging contract](https://developers.smartrecruiters.com/docs/customer-overview)

## Coordinates and reads

Use the explicit company identifier from an employer's SmartRecruiters posting URL. A sourcing-plan target is `source=smartrecruiters`, `board=<companyIdentifier>`, plus the existing company/employer fields. Production targets use the existing `coordinates.board` boundary. No employer names enter matching logic.

The public index is `GET /v1/companies/{companyIdentifier}/postings?destination=PUBLIC&limit=100&offset=0`. The documented page maximum is 100. Advance by the returned content length and require the requested offset, stable totalFound, bounded pages/postings, and nonrepeating page signatures. Company indexes may change during reads; a changing total or incomplete pagination is partial evidence, never a complete empty board.

Detail hydration is `GET /v1/companies/{companyIdentifier}/postings/{postingId}`. Requests stay on the configured API origin; index `ref` URLs are provenance rather than arbitrary fetch destinations. The posting ID supplies stable source identity even if optional UUID evidence appears or disappears. Duplicate posting IDs hydrate once.

The optional title prefilter uses configured target roles and the same existing title normalization. It does not infer country, remote work, or description facts from missing index metadata. Shared collection without an explicit filter stays broad. Full SearchBrief matching and delivery freshness still run after normalization.

## Timestamp and workplace evidence

`releasedDate` means the date the public posting was released, not the original requisition creation date. Official documentation notes that changing published content requires reposting. A recent release must not be described as proof that the requisition itself is newly created.

Only timezone-bearing timestamps become posting-age evidence. Missing or naive values remain unknown. Never substitute discovery, update, or collection times. Existing <=24h delivery freshness and unknown-age rejection remain authoritative.

Country ISO codes normalize to deterministic full names; unmapped values do not assert eligibility. Structured remote/workplace evidence is preserved. `remote=false` alone does not prove onsite, and unknown metadata remains unknown. Descriptions and all original provider fields needed for provenance stay within normalized Job metadata.

## URLs and lifecycle

`postingUrl` identifies the published job page; `applyUrl` identifies the application process. Both come from the detail response. The live control gate requires a canonical SmartRecruiters board URL and a valid HTTPS application URL for every normalized control posting, independently of whether it matches the client brief. These checks establish supplied URL shape/origin coverage, not a completed application or a separate HTTP reachability audit.

The index lists active postings. Detail 404 and explicit inactive records are suppressed as vanished, since index/detail reads are not atomic. A board-level 404 means invalid target. Authentication/restriction, throttling, transport failure, malformed response, and provider errors remain distinguishable.

No numeric public request limit has been verified. Do not assume unlimited use. Default requests have a 15-second timeout and at most two connect retries. A target-wide hydration 401/403/429/5xx or transport error stops further hydration; propagate its failure category when no jobs were hydrated, otherwise retain verified jobs as partial. Request metrics count logical client calls, not transport-level connection attempts.

## Health, registry, and evidence

Reuse existing active, valid_empty, invalid, restricted, rate_limited, transient_failure, and malformed_response classifications. The existing health probe uses a one-item public index, while full collection verifies details. Production admission remains active-only with matching health provenance. This change does not modify the frozen historical target universe, health manifests, or `production_active_v1`.

The benchmark has two purposes: a small live provider-contract control, and a bounded employer sample using the unchanged software-remote-US SearchBrief. Capacity targets are documented public boards: [ServiceNow](https://jobs.smartrecruiters.com/ServiceNow/744000148862459-software-engineer), [Visa](https://jobs.smartrecruiters.com/Visa/744000112644773-senior-software-engineer), [Palo Alto Networks](https://jobs.smartrecruiters.com/PaloAltoNetworks2/744000093622729-staff-engineer-software), and [Western Digital](https://jobs.smartrecruiters.com/westerndigital/744000150594619). The benchmark itself verifies current API behavior; these public pages are coordinate evidence, not evidence of posting freshness.

Capacity reports expose provider totals, index age/freshness, plausible title candidates, hydration/request counts, full matches, canonical/apply coverage, runtime, and target failure categories. Dry-run deliverable counts come from the existing normalized inventory, matcher, dedupe, and batch assembly, preserving the brief's employer cap. History replay uses an isolated ledger and exports only a temporary local CSV; it does not write a client Sheet or load production client history. It therefore measures bounded supply and replay behavior, not incremental net yield over existing production inventory.

A truncated sample is a lower-bound observation, not a daily market estimate. No evidence here establishes 2,000 deliverable jobs/day. Other providers should follow measured incremental yield, not this provider's raw posting total.

## Recorded live result

Run [36926588436](https://github.com/NicoCipher/JobSift/actions/runs/36926588436), evaluated at head `1729049e40f80b8825b0ca6fc4c43cf187eaf270`, completed the four-board sample without target failures or truncation. ServiceNow had 707 public postings; Western Digital had 326; Visa and PaloAltoNetworks2 were valid empty at this observation.

All 1,033 index records had timezone-bearing posting timestamps; 38 were <=24h old. Title prefiltering retained 125 and skipped 908. All 125 hydrated successfully with canonical/apply URL coverage; six hydrated postings were <=24h old. Ten postings matched the semantic brief, but none also passed freshness, so the unchanged batch pipeline selected zero deliverable jobs and zero qualifying employers. Fourteen index calls plus 125 detail calls took 64.4 seconds in total target runtime.

Reports and their original JSON digests are retained under `validation/smartrecruiters_provider_v1/evidence/`; the artifact ZIP digest is `aaa887d31ab1222b14146741c1ccc8fcf93bd35cbbb49faefc3ac56d35678416`. The earlier 500-record ServiceNow sample is also retained with its partial classification, rather than replaced with an inflated scale claim.

This sample establishes a working contract and zero fresh qualifying yield for these four boards at this time. It cannot establish provider-wide daily capacity. Additional verified boards and repeated independent observations are needed before attributing meaningful contribution toward the 2,000/day ceiling. No criteria were relaxed to produce this result.
