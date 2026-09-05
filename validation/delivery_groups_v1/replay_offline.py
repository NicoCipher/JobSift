"""Replay preserved v5 canonical postings through the production delivery pipeline.

Requires the external databases identified in operator_style_v1/results/board_metrics.csv.
Run from the repository root: python -m validation.delivery_groups_v1.replay_offline
No live collectors, source normalization, or historical database migrations are invoked.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from job_scout.dedupe.resolver import DEDUPE_VERSION
from job_scout.domain.models import CollectionResult, Job, SourceTarget
from job_scout.export.csv_exporter import CSV_COLUMNS
from job_scout.matching import matcher
from job_scout.orchestration.pipeline import run_pipeline
from job_scout.search_brief import load_search_brief
from job_scout.storage.sqlite import SQLiteRepository

ROOT = Path(__file__).resolve().parents[2]
PRIOR = ROOT / "validation/operator_style_v1/results"
OUTPUT = Path(__file__).resolve().parent / "results"
BRIEF = ROOT / "config/search_briefs/taiwo_operator_sourcing_v1.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FrozenCollector:
    def __init__(self, jobs: list[Job]):
        self.jobs = jobs

    def collect(self, target: SourceTarget) -> CollectionResult:
        return CollectionResult(
            source="preserved_evidence", target=target, jobs=self.jobs, status="success"
        )


def decisions(connection: sqlite3.Connection) -> list[tuple]:
    # Exclude evaluation timestamps only. Matching semantics must be byte-identical.
    return [
        tuple(row)
        for row in connection.execute(
            "SELECT job_id, client_id, decision, score, matched_reasons_json, "
            "rejection_reasons_json, matcher_version FROM job_matches ORDER BY job_id, client_id"
        )
    ]


def main() -> None:
    assert matcher.MATCHER_VERSION == "deterministic-v5"
    print(f"MATCHER_VERSION == {matcher.MATCHER_VERSION!r}")
    brief = load_search_brief(BRIEF)
    assert sha256(BRIEF) == json.loads((PRIOR / "provenance.json").read_text())["brief_sha256"]
    with (PRIOR / "board_metrics.csv").open(newline="") as handle:
        manifest = list(csv.DictReader(handle))
    inputs = []
    for row in manifest:
        path = ROOT / row["database"]
        assert sha256(path) == row["database_sha256"], path
        inputs.append({"path": str(path.relative_to(ROOT)), "sha256": sha256(path)})
    # Refuse to overwrite any prior evaluation.
    OUTPUT.mkdir(parents=True, exist_ok=False)
    totals: dict[str, Counter] = {}
    board_results = []
    surfaced_groups = []
    for row in manifest:
        assert matcher.MATCHER_VERSION == "deterministic-v5"
        path = ROOT / row["database"]
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as old:
            jobs = [
                Job.model_validate_json(item[0])
                for item in old.execute("SELECT payload_json FROM jobs ORDER BY id")
            ]
            old_decisions = decisions(old)
        directory = OUTPUT / row["source_family"] / row["board"]
        directory.mkdir(parents=True)
        db, csv_path = directory / "jobs.sqlite3", directory / "jobs.csv"
        repo = SQLiteRepository(db)
        result = run_pipeline(
            collector=FrozenCollector(jobs),
            target=SourceTarget(
                board_id=row["board"], company=jobs[0].company if jobs else row["board"]
            ),
            profile=brief,
            repository=repo,
            csv_path=csv_path,
        )
        with repo.connect() as c:
            assert decisions(c) == old_decisions, f"Matching changed: {row['board']}"
            assert not c.execute("PRAGMA foreign_key_check").fetchall()
            source_postings = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            versions = {r[0] for r in c.execute("SELECT DISTINCT matcher_version FROM job_matches")}
            assert not jobs or versions == {"deterministic-v5"}
            assert source_postings == len(jobs) == int(row["jobs"])
            matched_groups = [
                r[0]
                for r in c.execute(
                    "SELECT DISTINCT g.group_id FROM posting_delivery_groups g "
                    "JOIN job_matches m ON m.job_id=g.job_id "
                    "WHERE m.decision IN ('strong_match', 'possible_match')"
                )
            ]
            for group_id in matched_groups:
                selected = c.execute(
                    "SELECT job_id FROM group_deliveries WHERE group_id=?", (group_id,)
                ).fetchone()[0]
                members = []
                for member in c.execute(
                    "SELECT j.* FROM jobs j JOIN posting_delivery_groups g ON g.job_id=j.id "
                    "WHERE g.group_id=? ORDER BY j.source, j.source_job_id",
                    (group_id,),
                ):
                    job = Job.model_validate_json(member["payload_json"])
                    members.append(
                        {
                            "job_id": job.id,
                            "source": job.source,
                            "board": job.source_board_id,
                            "source_job_id": job.source_job_id,
                            "title": job.title,
                            "job_url": str(job.job_url),
                            "canonical_url": str(job.canonical_url),
                            "apply_url": str(job.apply_url) if job.apply_url else None,
                            "first_seen_at": member["first_seen_at"],
                            "last_seen_at": member["last_seen_at"],
                            "content_fingerprint": job.content_fingerprint,
                            "description_sha256": hashlib.sha256(
                                (job.description_text or "").encode()
                            ).hexdigest(),
                            "posted_at": job.posted_at.isoformat() if job.posted_at else None,
                            "selected_for_delivery": job.id == selected,
                        }
                    )
                surfaced_groups.append({"group_id": group_id, "members": members})
            assert (
                c.execute("SELECT COUNT(*) FROM group_deliveries").fetchone()[0] == result.exported
            )
        with csv_path.open(newline="") as handle:
            reader = csv.DictReader(handle)
            assert reader.fieldnames == CSV_COLUMNS
            assert len(list(reader)) == result.exported == len(matched_groups)
        metrics = {
            "source_postings": source_postings,
            "matched_postings": result.matched,
            "matched_delivery_groups": len(matched_groups),
            "delivered_leads": result.exported,
            "suppressed_duplicate_deliveries": result.matched - result.exported,
        }
        totals.setdefault(row["source_family"], Counter()).update(metrics)
        board_results.append(
            {
                "source_family": row["source_family"],
                "board": row["board"],
                **metrics,
                "database_sha256": sha256(db),
                "csv_sha256": sha256(csv_path),
            }
        )
    assert totals["greenhouse"]["source_postings"] == 2651
    assert totals["ashby_preserved_audit"]["source_postings"] == 797
    assert totals["greenhouse"]["matched_postings"] == 4
    assert totals["greenhouse"]["delivered_leads"] == 3
    assert totals["ashby_preserved_audit"]["delivered_leads"] == 0
    for item in inputs:
        assert sha256(ROOT / item["path"]) == item["sha256"]
    report = {
        "baseline_commit": "0525ee70f2194ee103c59fefeee857ae1d898221",
        "matcher_version": matcher.MATCHER_VERSION,
        "dedupe_version": DEDUPE_VERSION,
        "matching_decisions_identical": True,
        "aggregates": totals,
        "boards": board_results,
    }
    (OUTPUT / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    (OUTPUT / "surfaced_groups.json").write_text(json.dumps(surfaced_groups, indent=2) + "\n")
    source_files = [
        "job_scout/dedupe/resolver.py",
        "job_scout/storage/sqlite.py",
        "job_scout/orchestration/pipeline.py",
        "job_scout/matching/matcher.py",
        "job_scout/domain/models.py",
        "validation/delivery_groups_v1/replay_offline.py",
    ]
    provenance = {
        "created_at_utc": datetime.now(UTC).isoformat(),
        "live_calls": False,
        "python": sys.executable,
        "matcher_module": matcher.__file__,
        "matcher_version": matcher.MATCHER_VERSION,
        "dedupe_version": DEDUPE_VERSION,
        "brief_sha256": sha256(BRIEF),
        "inputs": inputs,
        "code_sha256": {p: sha256(ROOT / p) for p in source_files},
    }
    (OUTPUT / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(json.dumps(totals, indent=2))


if __name__ == "__main__":
    main()
