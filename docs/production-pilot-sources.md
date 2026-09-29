# Production pilot sources

Verified on 2026-09-29 for the first live JobSift software-engineering pilot.

This file records why each source target is trusted. The sourcing plan itself stays within the strict `sourcing-plan-v1` contract.

| Company | JobSift target | US-company evidence | Live board evidence |
| --- | --- | --- | --- |
| GitLab | `greenhouse:gitlab` | GitLab Inc. states in its 2026 Form 10-K that it is incorporated in Delaware and is remote-only. | `https://job-boards.greenhouse.io/gitlab/` currently lists multiple software/backend engineering roles with Remote, United States locations. |
| Reddit | `greenhouse:reddit` | Reddit, Inc. is a Delaware corporation with principal executive offices in San Francisco, California, per its 2026 SEC filings. | `https://job-boards.greenhouse.io/reddit/` currently exposes Remote - United States software-engineering postings. |
| Render | `ashby:render` | Render's official About page says it is headquartered in San Francisco, California. | `https://jobs.ashbyhq.com/render/` currently exposes multiple Remote: United States software-engineering postings. |

## Pilot rule

The production brief requires both explicit United States market evidence and explicit remote work. Hybrid, on-site, unknown-country, and unknown-work-mode results are not eligible for automatic batch delivery.

Work authorization and sponsorship eligibility are deliberately not used as sourcing filters for this client.

## Evidence references

- GitLab corporate evidence: https://www.sec.gov/Archives/edgar/data/1653482/000162828026018731/gtlb-20260131.htm
- GitLab jobs: https://job-boards.greenhouse.io/gitlab/
- Reddit corporate evidence: https://www.sec.gov/Archives/edgar/data/1713445/000171344526000107/rddt-20260812.htm
- Reddit jobs: https://job-boards.greenhouse.io/reddit/
- Render company evidence: https://render.com/about
- Render jobs: https://jobs.ashbyhq.com/render/
