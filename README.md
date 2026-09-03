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

```bash
python -m job_scout.cli collect \
  --client config/clients/example.json \
  --board example-board-token \
  --company "Verified Company Name" \
  --database jobs.sqlite3 \
  --csv exports/jobs.csv
```

The company name is configuration evidence because the Greenhouse list endpoint does not guarantee a company-name field. A source failure returns an explicit status and never acts as evidence that prior jobs closed.

