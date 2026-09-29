# US remote software pilot

The requested output is 100 fresh direct links per day for software engineering
or developer roles at US companies. Only fully remote roles qualify. Hybrid,
on-site, and unknown work modes do not qualify for release. Applicant residence,
citizenship, sponsorship, and work authorization are outside this sourcing brief.

The pilot SearchBrief is
`config/search_briefs/us_remote_software_engineering_v1.json`. It requires a
US job market and remote work mode. A US job market does **not** prove that the
employer is a US company; the initial source list is separately grounded:

| Employer | Board | Company evidence | Careers evidence |
| --- | --- | --- | --- |
| Render | Ashby `render` | https://render.com/about (San Francisco headquarters) | https://render.com/careers |
| GitLab Inc. | Greenhouse `gitlab` | https://about.gitlab.com/company/visiting/ (US corporate address) | https://job-boards.greenhouse.io/gitlab |

The two-board plan is a **capacity probe**, not a claim that it yields 100 daily
links. Expand the verified company universe only with official company evidence
and a working direct board. Preserve partial and failed source reports. Do not
fill a shortfall with hybrid, on-site, unknown-mode, stale, or fabricated jobs.

## First run boundary

1. Run `job-scout source --plan config/sourcing_plans/us_remote_software_pilot_v1.json`
   on a persistent machine. Inspect the JSON report for failures and review the
   stored jobs, decisions, and live direct URLs.
2. The current source command exports to its own local CSV. It does not prepare
   or publish a Google Sheets batch. Do not copy that CSV blindly into the sheet.
3. Create an explicit candidate set from the observed run, review every selected
   role for fully remote evidence and US company provenance, and prepare a batch
   with `prepare_daily_batch`. The current CLI has no batch-prepare command: that
   interface must be added with an authoritative run scope before unattended use.
4. Review the frozen prepared batch using `job-scout batch review`. Release only
   after inspecting the destination, selected links, completeness, and shortfall.

Configure the authorized spreadsheet ID and `Sheet1` tab as a `gsheet://`
destination outside source control. The dedicated service account has Editor access. Google Application Default
Credentials must be provided by the host runtime identity; this workspace's
Google Drive connector is not a Python credential. Use one persistent SQLite
database for this destination and back it up before scheduling runs.

No VM or paid Google Cloud resource is provisioned by this repository change.
