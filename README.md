# JobSift

JobSift is a standalone, deterministic job-sourcing engine for an operator who sources tailored vacancies for clients. SQLite is the system of record; CSV is the first delivery surface. It has no BIA dependency and uses no LLM.

## Current scope

Posting preservation, duplicate confidence, migration and client-specific delivery
suppression are documented in [Delivery groups](docs/delivery_groups.md).

- Strict canonical job and sourcing-brief contracts
- Greenhouse and Ashby public Job Board API adapters
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

Create a validated `SearchBrief` through operator-friendly prompts:

```bash
python -m job_scout profile create
```

The brief describes the jobs the client wants sourced. Target job market, candidate residence, and optional work-country eligibility are separate facts. Candidate residence is informational unless the operator explicitly enables work-eligibility filtering.

Rules use four explicit intents: `must`, `prefer`, `avoid`, and `ignore`. The normal flow keeps experience, employment type, and work-eligibility filtering under optional advanced restrictions. Missing preferred terms never reject a relevant job.

The optional maximum required experience is an operator eligibility ceiling, not a claim about the candidate's own experience. When configured, only a clearly mandatory minimum above that ceiling rejects a job. Preferred, ambiguous, unrelated, or absent experience evidence does not create a hard rejection. Existing `CandidateProfile` JSON remains loadable through an explicit compatibility translation; old country constraints retain their work-eligibility meaning and are never silently reinterpreted as target markets.

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

Use `--source ashby` for an Ashby board. Omitting `--source` continues to select
Greenhouse. Both sources can use the same database and CSV for shared delivery history:

```bash
python -m job_scout collect --source ashby \
  --client config/search_briefs/taiwo_operator_sourcing_v1.json \
  --board supabase --company Supabase \
  --database jobs.sqlite3 --csv exports/taiwo.csv
```

See [Ashby contract and validation](docs/ashby.md) for identity precedence, structured
locations, workplace conflicts, failure handling and the frozen evaluation workflow.

## Greenhouse eligibility evidence

- **Structured/direct:** job location, associated office names/locations, title, description, departments, timestamps, URL, and board-defined metadata.
- **Deterministic text:** explicit Nigeria, India, United Kingdom, United States, and Canada evidence; `City, ST` US locations; explicit remote, hybrid, and on-site wording. Several explicit countries are retained without collapsing them into one.
- **Ambiguous:** `Remote`, `Americas`, `Global`, generic office/team names, or conflicting work-mode statements. These do not establish a country; specific hybrid/on-site evidence overrides weaker remote wording.
- **Unavailable as a standard field:** employment type. It remains unknown unless an exact `Employment Type` custom metadata field contains a recognized value.

Remote status and work-country eligibility are evaluated independently. A remote vacancy restricted to a different country is not treated as globally eligible.

Under a new `SearchBrief`, target market is evaluated independently from candidate residence. Citizenship and work-authorization wording affects sourcing only when the brief explicitly enables work-eligibility filtering or adds the wording as an avoid term.
