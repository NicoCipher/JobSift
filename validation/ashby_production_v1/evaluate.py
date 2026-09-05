"""Bounded audit driver, not production multi-board orchestration.

python -m validation.ashby_production_v1.evaluate --phase offline
python -m validation.ashby_production_v1.evaluate --phase live --commit <published SHA>
Each board uses the production collector and pipeline with its own fresh DB/CSV.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx

from job_scout.collectors.ashby import AshbyCollector
from job_scout.dedupe.resolver import DEDUPE_VERSION
from job_scout.domain.models import CollectionResult, Job, SourceTarget
from job_scout.export.csv_exporter import CSV_COLUMNS
from job_scout.matching.matcher import MATCHER_VERSION
from job_scout.orchestration.pipeline import run_pipeline
from job_scout.search_brief import load_search_brief
from job_scout.storage.sqlite import SQLiteRepository

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
RAW = ROOT.parent / "ashby_coverage_audit_2026-09-03/raw"
BRIEF = ROOT / "config/search_briefs/taiwo_operator_sourcing_v1.json"
COHORT = HERE / "cohort.csv"
EXPECTED_BOARDS = (
    "1password",
    "airbyte",
    "claylabs",
    "clickhouse",
    "linear",
    "menlosecurity",
    "pleo",
    "posthog",
    "render",
    "satispay",
    "sentry",
    "supabase",
    "temporal",
    "vanta",
    "zapier",
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


class ObservedCollector:
    """Capture the production result for audit metrics without changing the pipeline."""

    source = "ashby"

    def __init__(self, collector: AshbyCollector):
        self.collector = collector
        self.result: CollectionResult | None = None

    def collect(self, target: SourceTarget) -> CollectionResult:
        self.result = self.collector.collect(target)
        return self.result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("offline", "live"), required=True)
    parser.add_argument("--commit")
    args = parser.parse_args()
    if args.phase == "live" and (not args.commit or len(args.commit) != 40):
        parser.error("live validation requires the published 40-character commit SHA")
    assert MATCHER_VERSION == "deterministic-v5"
    assert DEDUPE_VERSION == "dedupe-v1"
    assert sha(BRIEF) == "7502c14a0dfc8ecdfaca9b2d1ba70a2c02aaec67aaf28bb495a5a7ba76d361f6"
    with COHORT.open(newline="") as handle:
        cohort = list(csv.DictReader(handle))
    assert tuple(row["board_token"] for row in cohort) == EXPECTED_BOARDS
    for row in cohort:
        assert sha(RAW / f"{row['board_token']}.json") == row["sha256"]
    if args.phase == "live":
        offline = json.loads((HERE / "offline/summary.json").read_text())
        assert offline["totals"]["raw_postings"] == 797
        assert offline["totals"]["normalized"] == 797
        assert offline["totals"].get("quarantined", 0) == 0
        assert offline["statuses"] == {"success": 15}
    output = HERE / args.phase
    output.mkdir(parents=True, exist_ok=False)
    code = [
        "job_scout/collectors/ashby.py",
        "job_scout/collectors/greenhouse.py",
        "job_scout/cli.py",
        "job_scout/matching/matcher.py",
        "job_scout/dedupe/resolver.py",
        "job_scout/storage/sqlite.py",
        "job_scout/orchestration/pipeline.py",
        "validation/ashby_production_v1/evaluate.py",
    ]
    manifest = {
        "started_at_utc": datetime.now(UTC).isoformat(),
        "phase": args.phase,
        "implementation_commit": args.commit,
        "baseline_commit": "e20787a1148bfb046f73802ceb44b01a2a520014",
        "python": sys.executable,
        "matcher_version": MATCHER_VERSION,
        "dedupe_version": DEDUPE_VERSION,
        "brief_sha256": sha(BRIEF),
        "cohort_sha256": sha(COHORT),
        "cohort": cohort,
        "code_sha256": {name: sha(ROOT / name) for name in code},
        "live_calls": args.phase == "live",
    }
    # Written before any live board request; the cohort cannot change mid-run.
    write_json(output / "freeze.json", manifest)
    brief = load_search_brief(BRIEF)
    boards = []
    lead_index = []
    totals: Counter = Counter()
    statuses: Counter = Counter()
    distributions = {
        key: Counter() for key in ("work_mode", "countries", "employment", "identity_source")
    }
    for row in cohort:
        assert MATCHER_VERSION == "deterministic-v5" and DEDUPE_VERSION == "dedupe-v1"
        board = row["board_token"]
        directory = output / board
        directory.mkdir()
        prior_raw = (RAW / f"{board}.json").read_bytes()
        previous = json.loads(prior_raw)
        captured = []
        if args.phase == "offline":
            client = httpx.Client(
                transport=httpx.MockTransport(
                    lambda request, body=prior_raw: httpx.Response(200, content=body)
                )
            )
            collector = AshbyCollector(client)
        else:
            collector = AshbyCollector()

        def capture(response: httpx.Response, captured=captured, directory=directory) -> None:
            response.read()
            captured.append(response.content)
            if args.phase == "live":
                (directory / f"response_{len(captured)}.json").write_bytes(response.content)

        collector.client.event_hooks["response"].append(capture)
        db = directory / "jobs.sqlite3"
        csv_path = directory / "jobs.csv"
        repository = SQLiteRepository(db)
        started = datetime.now(UTC).isoformat()
        observer = ObservedCollector(collector)
        try:
            result = run_pipeline(
                collector=observer,
                target=SourceTarget(board_id=board, company=row["company"]),
                profile=brief,
                repository=repository,
                csv_path=csv_path,
            )
        finally:
            collector.client.close()
        raw = None
        if captured:
            try:
                raw = json.loads(captured[-1])
            except ValueError:
                pass
        raw_jobs = raw.get("jobs", []) if isinstance(raw, dict) else []
        if not isinstance(raw_jobs, list):
            raw_jobs = []
        counts = Counter(
            {
                "raw_postings": collector.last_counts.get("received", 0),
                "listed_postings": sum(
                    isinstance(j, dict) and j.get("isListed") is True for j in raw_jobs
                ),
                "skipped_unlisted": collector.last_counts.get("skipped_unlisted", 0),
                "normalized": result.received,
                "quarantined": collector.last_counts.get("quarantined", 0),
                "delivered_leads": result.exported,
            }
        )
        with repository.connect() as c:
            assert not c.execute("PRAGMA foreign_key_check").fetchall()
            counts["delivery_groups"] = c.execute(
                "SELECT COUNT(*) FROM delivery_groups"
            ).fetchone()[0]
            delivered = {r[0] for r in c.execute("SELECT job_id FROM exports")}
            matched_groups = set()
            for match in c.execute("SELECT * FROM job_matches ORDER BY job_id"):
                job = Job.model_validate_json(
                    c.execute(
                        "SELECT payload_json FROM jobs WHERE id=?", (match["job_id"],)
                    ).fetchone()[0]
                )
                reasons = json.loads(match["rejection_reasons_json"])
                matched = json.loads(match["matched_reasons_json"])
                assert match["matcher_version"] == "deterministic-v5"
                counts[match["decision"]] += 1
                relevant = "title does not match a configured target role" not in reasons
                counts["target_role_relevant"] += int(relevant)
                counts["management_rejected"] += int(
                    any("management/executive title excluded" in r for r in reasons)
                )
                counts["target_market_conflicts"] += int(
                    any(r.startswith("target market countries") for r in reasons)
                )
                counts["work_mode_conflicts"] += int(
                    any(r.startswith("work mode ") for r in reasons)
                )
                counts["country_known"] += int(bool(job.eligible_countries))
                counts["country_unknown"] += int(not job.eligible_countries)
                counts["multi_country"] += int(len(job.eligible_countries) > 1)
                distributions["work_mode"][job.remote_status.value] += 1
                distributions["countries"].update(job.eligible_countries)
                distributions["employment"][
                    job.employment_type.value if job.employment_type else "unknown"
                ] += 1
                distributions["identity_source"][job.raw_metadata["identity_source"]] += 1
                group = repository.delivery_group_id(job.id)
                if match["decision"] in {"strong_match", "possible_match"}:
                    matched_groups.add(group)
                if relevant:
                    lead_index.append(
                        {
                            "board": board,
                            "company": row["company"],
                            "id": job.id,
                            "source_job_id": job.source_job_id,
                            "title": job.title,
                            "job_url": str(job.job_url),
                            "apply_url": str(job.apply_url) if job.apply_url else None,
                            "location": job.location_text,
                            "countries": sorted(job.eligible_countries),
                            "work_mode": job.remote_status.value,
                            "decision": match["decision"],
                            "rejection_reasons": reasons,
                            "matched_reasons": matched,
                            "delivery_group": group,
                            "delivered": job.id in delivered,
                        }
                    )
            counts["matched_delivery_groups"] = len(matched_groups)
            counts["duplicate_deliveries_suppressed"] = (
                counts["strong_match"] + counts["possible_match"] - result.exported
            )
        if csv_path.exists():
            with csv_path.open(newline="") as handle:
                reader = csv.DictReader(handle)
                assert reader.fieldnames == CSV_COLUMNS
                assert len(list(reader)) == result.exported
        old_ids = {j["id"] for j in previous["jobs"] if isinstance(j, dict) and j.get("id")}
        new_ids = {
            j["id"] for j in raw_jobs if isinstance(j, dict) and isinstance(j.get("id"), str)
        }
        comparable = (
            isinstance(raw, dict)
            and isinstance(raw.get("jobs"), list)
            and result.status in {"success", "partial"}
        )
        record = {
            "board": board,
            "company": row["company"],
            "status": result.status,
            "started_at_utc": started,
            "completed_at_utc": datetime.now(UTC).isoformat(),
            "counts": dict(counts),
            "database_sha256": sha(db),
            "csv_sha256": sha(csv_path) if csv_path.exists() else None,
            "response_sha256": hashlib.sha256(captured[-1]).hexdigest() if captured else None,
            "previous_raw_count": len(previous["jobs"]),
            "added_provider_ids": sorted(new_ids - old_ids) if comparable else None,
            "removed_provider_ids": sorted(old_ids - new_ids) if comparable else None,
            "errors": observer.result.errors
            if observer.result
            else ["collection did not complete"],
        }
        boards.append(record)
        statuses[result.status] += 1
        totals.update(counts)
        write_json(output / "boards.json", boards)
        write_json(output / "lead_index.json", lead_index)
        print(
            f"{board}: {result.status}; raw={counts['raw_postings']} normalized={result.received} delivered={result.exported}",
            flush=True,
        )
    summary = {
        "phase": args.phase,
        "statuses": dict(statuses),
        "totals": dict(totals),
        "distributions": distributions,
        "completed_at_utc": datetime.now(UTC).isoformat(),
    }
    write_json(output / "summary.json", summary)
    for row in cohort:
        assert sha(RAW / f"{row['board_token']}.json") == row["sha256"]
    if args.phase == "offline":
        assert totals["raw_postings"] == totals["normalized"] == 797
        assert totals["quarantined"] == 0 and statuses == {"success": 15}
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
