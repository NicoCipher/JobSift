# Production software sources V2

Verified for JobSift's expanded remote-US software sourcing plan.

The purpose of this set is not to claim broad market coverage. It is a bounded
production expansion that is large enough to exercise one-job-per-employer delivery
with a five-job quota while retaining fail-closed source behavior.

## Selection standard

Every target in this plan must have:

1. a supported public ATS contract that JobSift can collect directly;
2. current or recent provider-health evidence that the target is active;
3. evidence that the employer is a United States company or has its principal
   headquarters in the United States; and
4. current evidence of at least one software-engineering opening that is explicitly
   remote in the United States.

The SearchBrief still performs the posting-level remote and United States market
checks. Source curation is what enforces the separate "US company" requirement;
that corporate attribute is not currently inferred by the matcher.

## V2 targets

| Employer | Employer ID | JobSift target | Internal health evidence | Current remote-US software evidence |
| --- | --- | --- | --- | --- |
| GitLab | `gitlab` | `greenhouse:gitlab` | Active in JobSift provider health validation | Greenhouse currently exposes Remote, United States engineering roles |
| Reddit | `reddit` | `greenhouse:reddit` | Production pilot source | Greenhouse currently exposes Remote - United States software roles |
| Render | `render` | `ashby:render` | Active; 35 postings in health pilot | Ashby currently exposes Remote: United States software-engineering roles |
| LaunchDarkly | `launchdarkly` | `greenhouse:launchdarkly` | Active; 48 postings in full health validation | Senior Backend Engineer, Flag Delivery — Remote - US |
| Human Interest | `human-interest` | `greenhouse:humaninterest` | Active; 77 postings in full health validation | Senior Software Engineer — United States, Remote |
| Life360 | `life360` | `greenhouse:life360` | Active; 19 postings in full health validation | Multiple engineering roles — Remote, USA / Remote, Canada |
| Ramp | `ramp` | `ashby:ramp` | Active; 141 postings in full health validation | Software Engineer, Security, Stablecoin — Remote (US), location type Remote |

The full target-universe validation is dated 2026-09-09. The current vacancy
checks above were refreshed on 2026-09-30. Provider-health evidence is useful for
source reliability, but it is not a claim that any individual vacancy will remain
open later.

## US-company evidence

- GitLab: 2026 Form 10-K states GitLab Inc. is incorporated in Delaware.
- Reddit: 2026 SEC filings identify Reddit, Inc. as a Delaware corporation with
  principal executive offices in San Francisco.
- Render: official company material identifies San Francisco as headquarters.
- LaunchDarkly: LaunchDarkly's legal materials identify Catamorphic Co. dba
  LaunchDarkly at 1999 Harrison St., Oakland, California; archived terms identify
  it as a Delaware corporation.
- Human Interest: the official About page lists its headquarters at
  655 Montgomery Street, San Francisco, California.
- Life360: investor materials identify Life360, Inc. as a Delaware corporation and
  list company headquarters in San Mateo, California.
- Ramp: Ramp Business Corporation lists its New York, New York business address
  on its official site.

## Evidence references

- GitLab corporate: https://www.sec.gov/Archives/edgar/data/1653482/000162828026018731/gtlb-20260131.htm
- GitLab jobs: https://job-boards.greenhouse.io/gitlab/
- Reddit corporate: https://www.sec.gov/Archives/edgar/data/1713445/000171344526000107/rddt-20260812.htm
- Reddit jobs: https://job-boards.greenhouse.io/reddit/
- Render company: https://render.com/about
- Render jobs: https://jobs.ashbyhq.com/render/
- LaunchDarkly company: https://launchdarkly.com/about-us/
- LaunchDarkly legal address: https://launchdarkly.com/policies/data-processing-addendum/
- LaunchDarkly jobs: https://job-boards.greenhouse.io/launchdarkly/
- Human Interest company: https://humaninterest.com/about-us
- Human Interest jobs: https://job-boards.greenhouse.io/humaninterest/
- Life360 company: https://investors.life360.com/ir-resources/investor-faqs/
- Life360 jobs: https://job-boards.greenhouse.io/life360/
- Ramp company: https://ramp.com/about-us
- Ramp jobs: https://jobs.ashbyhq.com/ramp/

## Capacity rule

The V2 plan contains seven distinct employers. A five-job run can therefore
produce at most five employers from this source set, and may produce fewer if
fewer than five have posting-level matches at run time.

A shortfall is the correct result in that case. JobSift must not relax remote,
United States market, title, or one-job-per-employer rules merely to fill quota.

This plan is a validation step, not evidence that JobSift can yet sustain 25, 50,
or 100 distinct-employer deliveries. Expansion beyond V2 should be driven by
measured distinct-employer yield from production runs and the existing active
target universe.
