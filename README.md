# JobSift

JobSift is a standalone, deterministic job-sourcing engine for an operator who sources tailored vacancies for clients. SQLite is the system of record; CSV is the first delivery surface. It has no BIA dependency and uses no LLM.

## Current scope

- Strict canonical job and client contracts
- Greenhouse Job Board API adapter
- deterministic normalization, eligibility and ranking reasons
- SQLite identity, change tracking and export idempotency
- exact five-column CSV export
- fixture-based end-to-end tests

Greenhouse access was verified on 2026-09-03 against its [official Job Board API documentation](https://docs.greenhouse.io/job-board.html). The documentation states that GET job-board data is public and needs no authentication. No numeric GET rate limit or general attribution rule is documented there, so those are recorded as unknown rather than assumed.

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest -q
ruff check .
```

## Run

Create a validated client profile through operator-friendly prompts:

```bash
python -m job_scout profile create
```

Unknown country, work-mode, and constrained employment-type evidence defaults to manual review. The operator can select `Any` when a field should not constrain eligibility.

Collect one configured Greenhouse board:

```bash
python -m job_scout.cli collect \
  --client config/clients/example.json \
  --board example-board-token \
  --company "Verified Company Name" \
  --database jobs.sqlite3 \
  --csv exports/jobs.csv
```

The company name is configuration evidence because the Greenhouse list endpoint does not guarantee a company-name field. A source failure returns an explicit status and never acts as evidence that prior jobs closed.

## Greenhouse eligibility evidence

- **Structured/direct:** job location, associated office names/locations, title, description, departments, timestamps, URL, and board-defined metadata.
- **Deterministic text:** explicit Nigeria, India, United Kingdom, United States, and Canada evidence; `City, ST` US locations; explicit remote, hybrid, and on-site wording. Several explicit countries are retained without collapsing them into one.
- **Ambiguous:** `Remote`, `Americas`, `Global`, generic office/team names, or conflicting work-mode statements. These do not establish a country; specific hybrid/on-site evidence overrides weaker remote wording.
- **Unavailable as a standard field:** employment type. It remains unknown unless an exact `Employment Type` custom metadata field contains a recognized value.

Remote status and work-country eligibility are evaluated independently. A remote vacancy restricted to a different country is not treated as globally eligible.
