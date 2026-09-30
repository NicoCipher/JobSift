# Sourcing plans

A SearchBrief describes **what jobs a client wants**. A SourcingPlan describes
**where JobSift should look** for them. The plan runs configured public boards
through the existing collectors, matcher, SQLite persistence, delivery groups,
and CSV exporter; it does not change their behavior.

JobSift currently supports Greenhouse, Ashby, Workday, and Lever targets. A
plan is strict JSON: unknown fields, malformed source coordinates, and duplicate
technical target identities are rejected before collection begins.

```json
{
  "plan_version": "sourcing-plan-v1",
  "plan_id": "taiwo-daily",
  "search_brief": "../search_briefs/taiwo_operator_sourcing_v1.json",
  "database": "../../jobs.sqlite3",
  "csv": "../../exports/jobs.csv",
  "targets": [
    {"source": "greenhouse", "company": "Example", "employer_id": "example", "board": "example"},
    {"source": "ashby", "company": "Example", "employer_id": "example", "board": "example"},
    {"source": "workday", "company": "Example", "employer_id": "example", "host": "example.wd1.myworkdayjobs.com", "tenant": "example", "site": "External"},
    {"source": "lever", "company": "Example", "employer_id": "example", "instance": "global", "site": "example"}
  ]
}
```

Technical target identities are `greenhouse:<board>`, `ashby:<board>`,
`workday:<host>:<tenant>:<site>`, and `lever:<instance>:<site>`. Company is
reporting/display evidence, not source identity. `employer_id` is the stable
business identity used by delivery diversification across ATS adapters; production
plans should set it explicitly. It is intentionally separate from the technical
target identity so the same employer can move ATS providers without becoming a
new company to the delivery policy. Targets run in JSON list order.

Run a plan from the repository root:

```sh
python -m job_scout source --plan config/sourcing_plans/example_four_sources_v1.json
```

Relative brief, database, and CSV paths resolve from the plan's directory. The
aggregate JSON report is written under `runs/<plan_id>/`. All targets share the
same brief, SQLite database, CSV destination, persistence history, and delivery
history. A failed source target is recorded and does not stop later targets;
neither a failure nor an absent listing is a closure claim.

The existing `job-scout collect` command remains available for single-board
debugging and manual operation.
