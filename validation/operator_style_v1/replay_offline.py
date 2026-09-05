from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from job_scout.domain.models import EmploymentType, Job, MatchDecision, RemoteStatus
from job_scout.export.csv_exporter import write_csv
from job_scout.matching.matcher import MATCHER_VERSION, match_job
from job_scout.normalization.core import canonicalize_url, content_fingerprint
from job_scout.normalization.location import normalize_location
from job_scout.search_brief import load_search_brief
from job_scout.storage.sqlite import SQLiteRepository

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVALUATION_ROOT = Path(__file__).resolve().parent
RESULTS_ROOT = EVALUATION_ROOT / "results"
BRIEF_PATH = PROJECT_ROOT / "config/search_briefs/taiwo_operator_sourcing_v1.json"
GREENHOUSE_INPUT = PROJECT_ROOT / "validation/coverage"
ASHBY_INPUT = PROJECT_ROOT.parent / "ashby_coverage_audit_2026-09-03"

GREENHOUSE_BOARDS = (
    "airtable",
    "anthropic",
    "cloudflare",
    "datadog",
    "figma",
    "gitlab",
    "grafanalabs",
    "postman",
    "stripe",
    "vercel",
)
EMPLOYMENT_TYPES = {
    "FullTime": EmploymentType.FULL_TIME,
    "PartTime": EmploymentType.PART_TIME,
    "Contract": EmploymentType.CONTRACT,
    "Temporary": EmploymentType.TEMPORARY,
    "Intern": EmploymentType.INTERNSHIP,
    "Internship": EmploymentType.INTERNSHIP,
    "Freelance": EmploymentType.FREELANCE,
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _greenhouse_jobs(path: Path) -> list[Job]:
    with sqlite3.connect(path) as connection:
        return [
            Job.model_validate_json(row[0])
            for row in connection.execute("SELECT payload_json FROM jobs ORDER BY id")
        ]


def _ashby_companies() -> dict[str, str]:
    with (ASHBY_INPUT / "cohort_manifest.csv").open(newline="", encoding="utf-8") as handle:
        return {row["board_token"]: row["company"] for row in csv.DictReader(handle)}


def _preserved_remote_classifications() -> dict[str, str]:
    with (ASHBY_INPUT / "title_relevant_jobs.csv").open(newline="", encoding="utf-8") as handle:
        return {row["posting_id"]: row["remote_classification"] for row in csv.DictReader(handle)}


def _structured_country(job: dict[str, Any]) -> set[str]:
    countries: set[str] = set()
    address = job.get("address") or {}
    postal = address.get("postalAddress") or {}
    if postal.get("addressCountry"):
        countries.add(str(postal["addressCountry"]))
    for secondary in job.get("secondaryLocations") or []:
        address = secondary.get("address") or {}
        postal = address.get("postalAddress") or address
        if postal.get("addressCountry"):
            countries.add(str(postal["addressCountry"]))
    countries.update(normalize_location(str(job.get("location") or "")).countries)
    return countries


def _ashby_remote(job: dict[str, Any], preserved: dict[str, str]) -> RemoteStatus:
    workplace = job.get("workplaceType")
    if workplace == "Remote":
        return RemoteStatus.REMOTE
    if workplace == "Hybrid":
        return RemoteStatus.HYBRID
    if workplace == "OnSite":
        return RemoteStatus.ONSITE
    location = str(job.get("location") or "")
    if location.casefold().startswith("remote"):
        return RemoteStatus.REMOTE
    if preserved.get(str(job.get("id"))) == "yes":
        return RemoteStatus.REMOTE
    return RemoteStatus.UNKNOWN


def _ashby_jobs(slug: str, company: str, preserved: dict[str, str]) -> list[Job]:
    data = json.loads((ASHBY_INPUT / f"raw/{slug}.json").read_text(encoding="utf-8"))
    jobs = []
    for raw in data["jobs"]:
        description = raw.get("descriptionPlain") or None
        location = raw.get("location") or None
        job_url = raw.get("jobUrl") or raw.get("applyUrl")
        apply_url = raw.get("applyUrl") or None
        countries = _structured_country(raw)
        employment_type = EMPLOYMENT_TYPES.get(str(raw.get("employmentType") or ""))
        source_job_id = str(raw["id"])
        jobs.append(
            Job(
                id=f"ashby-audit:{slug}:{source_job_id}",
                source="ashby_audit",
                source_job_id=source_job_id,
                source_board_id=slug,
                title=str(raw["title"]),
                company=company,
                description_text=description,
                description_html=raw.get("descriptionHtml") or None,
                job_url=job_url,
                apply_url=apply_url,
                canonical_url=canonicalize_url(str(job_url)),
                location_text=location,
                eligible_countries=countries,
                remote_status=_ashby_remote(raw, preserved),
                employment_type=employment_type,
                department=raw.get("department") or None,
                posted_at=raw.get("publishedAt") or None,
                content_fingerprint=content_fingerprint(
                    title=str(raw["title"]),
                    description=description,
                    location=location,
                    employment_type=employment_type,
                ),
                raw_metadata={
                    "is_listed": raw.get("isListed"),
                    "is_remote": raw.get("isRemote"),
                    "workplace_type": raw.get("workplaceType"),
                },
                discovered_via="preserved_ashby_coverage_audit",
            )
        )
    return jobs


def _run_board(*, source_family: str, board: str, jobs: list[Job]) -> dict[str, Any]:
    board_root = RESULTS_ROOT / source_family / board
    board_root.mkdir(parents=True, exist_ok=False)
    database = board_root / "jobs.sqlite3"
    csv_path = board_root / "surfaced.csv"
    repository = SQLiteRepository(database)
    brief = load_search_brief(BRIEF_PATH)
    decisions: Counter[str] = Counter()
    surfaced: list[Job] = []
    title_relevant = 0
    management_rejected = 0
    hard_market_work_mode_rejected = 0
    result_rows: list[dict[str, Any]] = []

    for job in jobs:
        repository.upsert_job(job)
        result = match_job(job, brief)
        assert result.matcher_version == "deterministic-v5"
        repository.save_match(result)
        decisions[result.decision.value] += 1
        wrong_title = "title does not match a configured target role" in result.rejection_reasons
        title_relevant += int(not wrong_title)
        management_rejected += int(
            any(
                "management/executive title excluded" in reason
                for reason in result.rejection_reasons
            )
        )
        hard_market_work_mode_rejected += int(
            any(
                reason.startswith(("target market", "work mode"))
                for reason in result.rejection_reasons
            )
        )
        if result.decision in {MatchDecision.STRONG_MATCH, MatchDecision.POSSIBLE_MATCH}:
            surfaced.append(job)
            result_rows.append(
                {
                    "source_family": source_family,
                    "board": board,
                    "job_id": job.id,
                    "company": job.company,
                    "title": job.title,
                    "location": job.location_text or "",
                    "countries": "; ".join(sorted(job.eligible_countries)),
                    "remote_status": job.remote_status.value,
                    "decision": result.decision.value,
                    "matched_reasons": " | ".join(result.matched_reasons),
                    "job_url": str(job.job_url),
                }
            )

    exported = write_csv(csv_path, surfaced)
    destination = str(csv_path.resolve())
    for job in surfaced:
        repository.mark_exported(job.id, brief.client_id, destination)

    with repository.connect() as connection:
        versions = {
            row[0] for row in connection.execute("SELECT DISTINCT matcher_version FROM job_matches")
        }
        persisted_jobs = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        persisted_matches = connection.execute("SELECT COUNT(*) FROM job_matches").fetchone()[0]
        persisted_exports = connection.execute("SELECT COUNT(*) FROM exports").fetchone()[0]
    assert versions == {"deterministic-v5"}
    assert persisted_jobs == len(jobs)
    assert persisted_matches == len(jobs)
    assert persisted_exports == exported == len(surfaced)

    _write_csv(board_root / "surfaced_detail.csv", result_rows)
    return {
        "source_family": source_family,
        "board": board,
        "jobs": len(jobs),
        "target_role_relevant": title_relevant,
        "management_executive_rejected": management_rejected,
        "hard_market_work_mode_rejected": hard_market_work_mode_rejected,
        "strong_match": decisions[MatchDecision.STRONG_MATCH.value],
        "possible_match": decisions[MatchDecision.POSSIBLE_MATCH.value],
        "needs_review": decisions[MatchDecision.NEEDS_REVIEW.value],
        "reject": decisions[MatchDecision.REJECT.value],
        "surfaced": len(surfaced),
        "exported": exported,
        "database": str(database.relative_to(PROJECT_ROOT)),
        "csv": str(csv_path.relative_to(PROJECT_ROOT)),
        "database_sha256": _sha256(database),
        "csv_sha256": _sha256(csv_path),
    }


def main() -> None:
    assert MATCHER_VERSION == "deterministic-v5"
    if RESULTS_ROOT.exists():
        raise RuntimeError(f"fresh replay destination already exists: {RESULTS_ROOT}")

    brief = load_search_brief(BRIEF_PATH)
    assert brief.client_id == "taiwo_operator_sourcing_v1"
    rows: list[dict[str, Any]] = []
    input_hashes: list[dict[str, str]] = []

    for board in GREENHOUSE_BOARDS:
        source = GREENHOUSE_INPUT / f"{board}.sqlite3"
        input_hashes.append(
            {"path": str(source.relative_to(PROJECT_ROOT)), "sha256": _sha256(source)}
        )
        rows.append(
            _run_board(
                source_family="greenhouse",
                board=board,
                jobs=_greenhouse_jobs(source),
            )
        )

    companies = _ashby_companies()
    preserved_remote = _preserved_remote_classifications()
    for board, company in companies.items():
        source = ASHBY_INPUT / f"raw/{board}.json"
        input_hashes.append({"path": str(source), "sha256": _sha256(source)})
        rows.append(
            _run_board(
                source_family="ashby_preserved_audit",
                board=board,
                jobs=_ashby_jobs(board, company, preserved_remote),
            )
        )

    _write_csv(RESULTS_ROOT / "board_metrics.csv", rows)
    aggregates = []
    numeric_fields = (
        "jobs",
        "target_role_relevant",
        "management_executive_rejected",
        "hard_market_work_mode_rejected",
        "strong_match",
        "possible_match",
        "needs_review",
        "reject",
        "surfaced",
        "exported",
    )
    for source_family in ("greenhouse", "ashby_preserved_audit"):
        selected = [row for row in rows if row["source_family"] == source_family]
        aggregates.append(
            {
                "source_family": source_family,
                "boards": len(selected),
                **{field: sum(int(row[field]) for row in selected) for field in numeric_fields},
            }
        )
    _write_csv(RESULTS_ROOT / "aggregate_metrics.csv", aggregates)

    all_surfaced = []
    for path in sorted(RESULTS_ROOT.glob("*/*/surfaced_detail.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            all_surfaced.extend(csv.DictReader(handle))
    _write_csv(RESULTS_ROOT / "surfaced_all.csv", all_surfaced)

    provenance = {
        "created_at_utc": datetime.now(UTC).isoformat(),
        "matcher_version": MATCHER_VERSION,
        "brief_path": str(BRIEF_PATH.relative_to(PROJECT_ROOT)),
        "brief_sha256": _sha256(BRIEF_PATH),
        "live_calls": False,
        "greenhouse_jobs_expected": 2651,
        "ashby_jobs_expected": 797,
        "inputs": input_hashes,
    }
    (RESULTS_ROOT / "provenance.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    assert aggregates[0]["jobs"] == 2651
    assert aggregates[1]["jobs"] == 797
    print(json.dumps({"matcher_version": MATCHER_VERSION, "aggregates": aggregates}, indent=2))


if __name__ == "__main__":
    main()
