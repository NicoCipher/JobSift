"""Resumable, bounded Workday Stage B capacity replay.

The default CLI path is an offline manifest/status preflight.  Network work is
possible only through explicit ``--resume`` and only after continuity state has
been copied out of Stage A's immutable runtime database.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import shutil
import sqlite3
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from job_scout.collectors.workday import USER_AGENT, WorkdayCollector
from job_scout.dedupe.resolver import DEDUPE_VERSION, representative_key, select_preferred_url
from job_scout.domain.models import (
    CollectionStatus,
    MatchDecision,
    SearchBrief,
    SourceTarget,
    WorkdayTargetConfig,
)
from job_scout.export.csv_exporter import CSV_COLUMNS, export_row
from job_scout.matching.matcher import MATCHER_VERSION, match_job
from job_scout.storage.sqlite import SQLiteRepository

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CAPACITY_ROOT = Path(__file__).resolve().parents[1]
STAGE_ROOT = Path(__file__).resolve().parent
RUNTIME = STAGE_ROOT / "runtime"
MANIFEST_PATH = STAGE_ROOT / "manifest.json"
SUMMARY_PATH = STAGE_ROOT / "summary.json"
REPORT_PATH = STAGE_ROOT / "report.md"
LOCK = RUNTIME / "run.lock"
DATABASE = RUNTIME / "replay.sqlite3"
CONTINUITY = RUNTIME / "continuity.json"
INCREMENTAL_DELIVERIES = RUNTIME / "incremental_deliveries.csv"
HEALTH_ROOT = PROJECT_ROOT / "validation/target_universe_health_v1/full"
HEALTH_MANIFEST = HEALTH_ROOT / "manifest.json"
HEALTH_RESULTS = HEALTH_ROOT / "results.json"
STAGE_A_ROOT = CAPACITY_ROOT / "stage_a"
STAGE_A_MANIFEST = STAGE_A_ROOT / "manifest.json"
STAGE_A_SUMMARY = STAGE_A_ROOT / "summary.json"
STAGE_A_DATABASE = STAGE_A_ROOT / "runtime/replay.sqlite3"
BRIEF_PATH = PROJECT_ROOT / "config/search_briefs/taiwo_operator_sourcing_v1.json"

BASELINE_COMMIT = "d9c7089f6ef9e4a8f234907205eda2d2de80f8ce"
STAGE_A_BASELINE_FRESH = 44
EXPECTED_STAGE_A_JOBS = 48_373
EXPECTED_STAGE_A_HISTORICAL_LINKS = 11_107
BUSINESS_THRESHOLD = 200
PREFERRED_THRESHOLD = 250
DEFAULT_MAX_TARGETS = 5
DEFAULT_MAX_ESTIMATED_REQUESTS = 2000
MIN_DELAY = 0.5
CUMULATIVE_DESTINATION = "taiwo-sourcing-capacity-v1:stage-a-plus-stage-b"
SOURCES = ("workday",)
SYSTEMIC_FAILURES = {
    CollectionStatus.NETWORK_FAILURE.value,
    CollectionStatus.RATE_LIMITED.value,
    CollectionStatus.PROVIDER_ERROR.value,
}
TIER_RANK = {
    "exact_1_49": 1,
    "exact_50_99": 2,
    "exact_100_499": 3,
    "exact_500_plus": 4,
    "potentially_capped": 5,
}
EXPECTED_TIER_COUNTS = {
    "exact_1_49": 215,
    "exact_50_99": 81,
    "exact_100_499": 147,
    "exact_500_plus": 65,
    "potentially_capped": 28,
}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha_value(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def artifact_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _brief() -> SearchBrief:
    brief = SearchBrief.model_validate_json(BRIEF_PATH.read_text(encoding="utf-8"))
    if (
        brief.client_id != "taiwo_operator_sourcing_v1"
        or brief.target_market.intent.value != "must"
        or brief.target_market.countries != {"United States"}
        or brief.work_mode.intent.value != "must"
        or {mode.value for mode in brief.work_mode.modes} != {"remote"}
        or brief.management_roles.value != "avoid"
    ):
        raise ValueError("Stage B requires the frozen Taiwo sourcing brief")
    return brief


def _health_values() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    manifest = json.loads(HEALTH_MANIFEST.read_text(encoding="utf-8"))
    results = json.loads(HEALTH_RESULTS.read_text(encoding="utf-8"))
    unhashed = dict(manifest)
    manifest_sha = unhashed.pop("manifest_sha256", None)
    if manifest_sha != sha_value(unhashed) or results.get("manifest_sha256") != manifest_sha:
        raise ValueError("completed target health evidence is not manifest-bound")
    rows = results.get("results")
    if not isinstance(rows, list) or results.get("completed") != len(rows) or len(rows) != 2287:
        raise ValueError("completed target health evidence is incomplete")
    by_identity = {row.get("target_identity"): row for row in rows}
    if len(by_identity) != len(rows):
        raise ValueError("completed target health evidence has duplicate identities")
    return manifest, by_identity


def _stage_a_evidence() -> dict[str, str]:
    manifest = json.loads(STAGE_A_MANIFEST.read_text(encoding="utf-8"))
    summary = json.loads(STAGE_A_SUMMARY.read_text(encoding="utf-8"))
    unhashed = dict(manifest)
    manifest_sha = unhashed.pop("manifest_sha256", None)
    if manifest_sha != sha_value(unhashed):
        raise ValueError("Stage A manifest hash is invalid")
    if summary.get("manifest_sha256") != manifest_sha:
        raise ValueError("Stage A summary is not manifest-bound")
    if summary.get("fresh_unique_deliveries") != STAGE_A_BASELINE_FRESH:
        raise ValueError("Stage A fresh delivery baseline is not frozen at 44")
    if summary.get("stop_reason") != "all_stage_a_targets_completed":
        raise ValueError("Stage A completion evidence is not final")
    return {
        "stage_a_manifest_sha256": manifest_sha,
        "stage_a_manifest_file_sha256": artifact_sha(STAGE_A_MANIFEST),
        "stage_a_summary_sha256": artifact_sha(STAGE_A_SUMMARY),
    }


def _tier(inventory: int, exact: bool) -> str:
    if not exact:
        return "potentially_capped"
    if inventory <= 49:
        return "exact_1_49"
    if inventory <= 99:
        return "exact_50_99"
    if inventory <= 499:
        return "exact_100_499"
    return "exact_500_plus"


def _target_metadata(target: dict[str, Any], health: dict[str, Any]) -> dict[str, Any]:
    coordinates = target.get("coordinates")
    inventory = health.get("current_postings")
    exact = health.get("inventory_exact")
    if (
        not isinstance(coordinates, dict)
        or not all(
            isinstance(coordinates.get(key), str) and coordinates[key]
            for key in ("host", "tenant", "site")
        )
        or not isinstance(inventory, int)
        or inventory < 1
        or not isinstance(exact, bool)
    ):
        raise ValueError(f"invalid active Workday health target: {target.get('target_identity')}")
    tier = _tier(inventory, exact)
    estimates: dict[str, int | None]
    uncertainty: str | None
    if exact:
        list_pages = math.ceil(inventory / 20)
        estimates = {
            "estimated_list_requests": list_pages,
            "estimated_detail_requests": inventory,
            "estimated_total_requests": list_pages + inventory,
        }
        uncertainty = None
    else:
        estimates = {
            "estimated_list_requests": None,
            "estimated_detail_requests": None,
            "estimated_total_requests": None,
        }
        uncertainty = (
            "health total is at or above the 2000 cap threshold; live partition "
            "contract and complete request cost are unknown"
        )
    return {
        "source": "workday",
        "target_identity": target["target_identity"],
        "host": coordinates["host"],
        "tenant": coordinates["tenant"],
        "site": coordinates["site"],
        "company_hint": target.get("company_hint") or target["target_identity"],
        "historical_occurrence_count": target["historical_occurrence_count"],
        "health_current_postings": inventory,
        "inventory_exact": exact,
        "potentially_capped": not exact,
        "cost_tier": tier,
        **estimates,
        "uncertainty_reason": uncertainty,
    }


def make_manifest(*, generated_at: str) -> dict[str, Any]:
    health_manifest, health = _health_values()
    targets_by_identity = {
        target["target_identity"]: target for target in health_manifest["targets"]
    }
    targets = [
        _target_metadata(targets_by_identity[identity], result)
        for identity, result in health.items()
        if result.get("source") == "workday" and result.get("classification") == "active"
    ]
    targets.sort(
        key=lambda target: (
            TIER_RANK[target["cost_tier"]],
            -target["historical_occurrence_count"],
            target["health_current_postings"],
            target["target_identity"],
        )
    )
    tier_counts = dict(Counter(target["cost_tier"] for target in targets))
    if len(targets) != 536 or tier_counts != EXPECTED_TIER_COUNTS:
        raise ValueError(f"unexpected Stage B target selection: {len(targets)}, {tier_counts}")
    stage_a = _stage_a_evidence()
    manifest = {
        "validation": "taiwo-sourcing-capacity-v1",
        "stage": "b",
        "generated_at": generated_at,
        "baseline_commit": BASELINE_COMMIT,
        "sources": list(SOURCES),
        "search_brief_sha256": artifact_sha(BRIEF_PATH),
        "matcher_version": MATCHER_VERSION,
        "dedupe_version": DEDUPE_VERSION,
        "health_manifest_sha256": health_manifest["manifest_sha256"],
        "health_results_sha256": artifact_sha(HEALTH_RESULTS),
        **stage_a,
        "stage_a_baseline_fresh_deliveries": STAGE_A_BASELINE_FRESH,
        "thresholds": {
            "business": BUSINESS_THRESHOLD,
            "preferred": PREFERRED_THRESHOLD,
            "stage_b_increment_for_business": BUSINESS_THRESHOLD - STAGE_A_BASELINE_FRESH,
            "stage_b_increment_for_preferred": PREFERRED_THRESHOLD - STAGE_A_BASELINE_FRESH,
        },
        "ordering": {
            "tiers": [
                "exact_1_49",
                "exact_50_99",
                "exact_100_499",
                "exact_500_plus",
                "potentially_capped",
            ],
            "within_tier": [
                "historical_occurrence_count_desc",
                "health_current_postings_asc",
                "target_identity_asc",
            ],
            "case_distinct_coordinates_preserved": True,
        },
        "cost_estimate_interpretation": (
            "Exact-inventory estimates are frozen nominal logical client request estimates "
            "before retry overhead. Potentially capped targets have no safe complete-cost estimate."
        ),
        "targets": targets,
    }
    manifest["manifest_sha256"] = sha_value(manifest)
    return manifest


def verify_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    unhashed = dict(manifest)
    manifest_sha = unhashed.pop("manifest_sha256", None)
    if manifest_sha != sha_value(unhashed):
        raise ValueError("Stage B manifest hash is invalid")
    expected = make_manifest(generated_at=manifest["generated_at"])
    if manifest != expected:
        raise ValueError("Stage B manifest differs from frozen health and Stage A evidence")
    return manifest


def freeze_manifest(*, generated_at: str | None = None) -> dict[str, Any]:
    if MANIFEST_PATH.exists():
        return load_manifest()
    manifest = make_manifest(generated_at=generated_at or datetime.now(UTC).isoformat())
    write_json(MANIFEST_PATH, manifest)
    return manifest


def load_manifest() -> dict[str, Any]:
    if not MANIFEST_PATH.exists():
        raise ValueError(
            "Stage B manifest has not been frozen; use --freeze-manifest offline first"
        )
    return verify_manifest(json.loads(MANIFEST_PATH.read_text(encoding="utf-8")))


RUNTIME_SCHEMA = """
CREATE TABLE IF NOT EXISTS stage_b_target_runs (
 manifest_sha256 TEXT NOT NULL, target_identity TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('in_progress','completed')),
 collection_status TEXT, started_at TEXT NOT NULL, completed_at TEXT, result_json TEXT,
 PRIMARY KEY(manifest_sha256,target_identity)
);
CREATE TABLE IF NOT EXISTS stage_b_deliveries (
 manifest_sha256 TEXT NOT NULL, target_identity TEXT NOT NULL, group_id TEXT NOT NULL,
 job_id TEXT NOT NULL, decision TEXT NOT NULL, evidence_json TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(manifest_sha256,group_id)
);
CREATE TABLE IF NOT EXISTS stage_b_invocations (
 id TEXT PRIMARY KEY, manifest_sha256 TEXT NOT NULL, started_at TEXT NOT NULL,
 completed_at TEXT, config_json TEXT NOT NULL, attempted_targets_json TEXT NOT NULL,
 pause_reason TEXT, terminal_stop_reason TEXT, systemic_evidence_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_stage_b_targets ON stage_b_target_runs(manifest_sha256,state);
CREATE INDEX IF NOT EXISTS ix_stage_b_invocations ON stage_b_invocations(manifest_sha256,started_at DESC);
"""
STAGE_A_CLIENT_ID = "taiwo_operator_sourcing_v1"


def _runtime_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _ensure_runtime_schema() -> None:
    with _runtime_connection() as connection:
        connection.executescript(RUNTIME_SCHEMA)


def _has_runtime_schema() -> bool:
    if not DATABASE.exists():
        return False
    with sqlite3.connect(DATABASE) as connection:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stage_b_target_runs'"
            ).fetchone()
            is not None
        )


def _fingerprint_rows(
    connection: sqlite3.Connection, query: str, args: tuple[Any, ...] = ()
) -> str:
    digest = hashlib.sha256()
    for row in connection.execute(query, args):
        digest.update(canonical(list(row)).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _stage_a_database_state(path: Path) -> dict[str, Any]:
    """Inspect immutable Stage A only through an SQLite read-only URI."""
    if not path.exists():
        raise ValueError("Stage A replay database is required for Stage B continuity")
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        jobs = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        history = connection.execute("SELECT COUNT(*) FROM historical_job_links").fetchone()[0]
        groups = connection.execute("SELECT COUNT(*) FROM posting_delivery_groups").fetchone()[0]
        invalid_groups = connection.execute(
            "SELECT COUNT(*) FROM (SELECT j.id FROM jobs j LEFT JOIN posting_delivery_groups g "
            "ON g.job_id=j.id GROUP BY j.id HAVING COUNT(g.group_id)!=1)"
        ).fetchone()[0]
        empty_keys = connection.execute(
            "SELECT COUNT(*) FROM delivery_keys WHERE TRIM(value)='' "
        ).fetchone()[0]
        orphan_keys = connection.execute(
            "SELECT COUNT(*) FROM delivery_keys k LEFT JOIN jobs j ON j.id=k.job_id WHERE j.id IS NULL"
        ).fetchone()[0]
        orphan_groups = connection.execute(
            "SELECT COUNT(*) FROM posting_delivery_groups p LEFT JOIN jobs j ON j.id=p.job_id "
            "LEFT JOIN delivery_groups g ON g.id=p.group_id WHERE j.id IS NULL OR g.id IS NULL"
        ).fetchone()[0]
        deliveries = connection.execute(
            "SELECT destination,COUNT(DISTINCT group_id) FROM group_deliveries "
            "WHERE client_id=? GROUP BY destination",
            (STAGE_A_CLIENT_ID,),
        ).fetchall()
        if len(deliveries) != 1 or deliveries[0][1] != STAGE_A_BASELINE_FRESH:
            raise ValueError("Stage A delivery history does not contain exactly 44 groups")
        invariants = {
            "jobs": jobs,
            "historical_job_links": history,
            "posting_delivery_groups": groups,
            "invalid_group_assignments": invalid_groups,
            "empty_delivery_keys": empty_keys,
            "orphan_delivery_keys": orphan_keys,
            "orphan_posting_delivery_groups": orphan_groups,
            "foreign_key_check": "ok" if not foreign else "failed",
            "integrity_check": integrity,
            "stage_a_distinct_delivered_groups": deliveries[0][1],
        }
        expected = {
            "jobs": EXPECTED_STAGE_A_JOBS,
            "historical_job_links": EXPECTED_STAGE_A_HISTORICAL_LINKS,
            "posting_delivery_groups": EXPECTED_STAGE_A_JOBS,
            "invalid_group_assignments": 0,
            "empty_delivery_keys": 0,
            "orphan_delivery_keys": 0,
            "orphan_posting_delivery_groups": 0,
            "foreign_key_check": "ok",
            "integrity_check": "ok",
            "stage_a_distinct_delivered_groups": STAGE_A_BASELINE_FRESH,
        }
        if any(invariants[key] != value for key, value in expected.items()):
            raise ValueError("Stage A replay database continuity invariants are not satisfied")
        destination = deliveries[0][0]
        topology = {
            "posting_delivery_groups": _fingerprint_rows(
                connection, "SELECT job_id,group_id FROM posting_delivery_groups ORDER BY job_id"
            ),
            "delivery_keys": _fingerprint_rows(
                connection, "SELECT job_id,kind,value FROM delivery_keys ORDER BY job_id,kind,value"
            ),
            "stage_a_delivery_rows": _fingerprint_rows(
                connection,
                "SELECT group_id,client_id,destination,job_id,exported_at FROM group_deliveries "
                "WHERE client_id=? AND destination=? ORDER BY group_id,job_id",
                (STAGE_A_CLIENT_ID, destination),
            ),
        }
    return {
        "source_database_sha256": artifact_sha(path),
        "source_destination": destination,
        "invariants": invariants,
        "topology_fingerprints": topology,
    }


def _verify_copy(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Stage B replay backup integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Stage B replay backup has foreign-key violations")


def _seed_cumulative_delivery_history(path: Path, destination: str) -> int:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT OR IGNORE INTO group_deliveries SELECT group_id,client_id,?,job_id,exported_at "
            "FROM group_deliveries WHERE client_id=? AND destination=? ORDER BY exported_at,job_id",
            (CUMULATIVE_DESTINATION, STAGE_A_CLIENT_ID, destination),
        )
        count = connection.execute(
            "SELECT COUNT(DISTINCT group_id) FROM group_deliveries WHERE client_id=? AND destination=?",
            (STAGE_A_CLIENT_ID, CUMULATIVE_DESTINATION),
        ).fetchone()[0]
    if count != STAGE_A_BASELINE_FRESH:
        raise ValueError("Stage B cumulative destination does not represent 44 Stage A groups")
    return count


def bootstrap(manifest: dict[str, Any]) -> dict[str, Any]:
    """Back up continuity without ever opening Stage A through SQLiteRepository."""
    _brief()
    evidence = _stage_a_evidence()
    if any(manifest.get(key) != value for key, value in evidence.items()):
        raise ValueError("Stage B manifest does not match immutable Stage A evidence")
    source = _stage_a_database_state(STAGE_A_DATABASE)
    expected = {
        "manifest_sha256": manifest["manifest_sha256"],
        "cumulative_destination": CUMULATIVE_DESTINATION,
        **evidence,
        **source,
    }
    if DATABASE.exists() or CONTINUITY.exists():
        if not DATABASE.exists() or not CONTINUITY.exists():
            raise ValueError("incomplete Stage B continuity state; refuse to overwrite it")
        current = json.loads(CONTINUITY.read_text(encoding="utf-8"))
        if any(current.get(key) != value for key, value in expected.items()):
            raise ValueError("existing Stage B continuity state is incompatible")
        _verify_copy(DATABASE)
        _ensure_runtime_schema()
        return current
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(RUNTIME.parent).free < STAGE_A_DATABASE.stat().st_size * 2:
        raise ValueError("insufficient free disk space for an isolated Stage B replay backup")
    with (
        sqlite3.connect(f"file:{STAGE_A_DATABASE}?mode=ro", uri=True) as original,
        sqlite3.connect(DATABASE) as copy,
    ):
        original.backup(copy)
    if artifact_sha(STAGE_A_DATABASE) != source["source_database_sha256"]:
        DATABASE.unlink(missing_ok=True)
        raise ValueError("Stage A database changed during backup; continuity copy discarded")
    _verify_copy(DATABASE)
    copied = _stage_a_database_state(DATABASE)
    if any(
        copied[key] != source[key]
        for key in ("source_destination", "invariants", "topology_fingerprints")
    ):
        raise ValueError("Stage B backup topology does not match immutable Stage A")
    _ensure_runtime_schema()
    seeded = _seed_cumulative_delivery_history(DATABASE, source["source_destination"])
    continuity = {
        **expected,
        "backup_initial_sha256": artifact_sha(DATABASE),
        "cumulative_delivery_group_count": seeded,
        "bootstrapped_at": datetime.now(UTC).isoformat(),
    }
    write_json(CONTINUITY, continuity)
    return continuity


class CountingClient:
    """This counts injected client calls; HTTPTransport retries are intentionally unknown."""

    def __init__(self, client: httpx.Client) -> None:
        self.client = client
        self.client_request_calls = 0

    def get(self, *args: Any, **kwargs: Any) -> httpx.Response:
        self.client_request_calls += 1
        return self.client.get(*args, **kwargs)

    def post(self, *args: Any, **kwargs: Any) -> httpx.Response:
        self.client_request_calls += 1
        return self.client.post(*args, **kwargs)

    def close(self) -> None:
        self.client.close()


def _collector(delay: float) -> tuple[WorkdayCollector, CountingClient]:
    client = CountingClient(
        httpx.Client(
            timeout=httpx.Timeout(20.0),
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            transport=httpx.HTTPTransport(retries=2),
        )
    )
    return WorkdayCollector(client=client, delay=delay), client


def _source_target(target: dict[str, Any]) -> SourceTarget:
    config = WorkdayTargetConfig(host=target["host"], tenant=target["tenant"], site=target["site"])
    return SourceTarget(board_id=config.board_id, company=target["company_hint"], workday=config)


def _historical_kind_connection(
    connection: sqlite3.Connection, job: Any, client_id: str
) -> str | None:
    if connection.execute(
        "SELECT 1 FROM historical_job_links WHERE client_id=? AND source=? AND source_board_id=? AND source_job_id=? LIMIT 1",
        (client_id, job.source, job.source_board_id, job.source_job_id),
    ).fetchone():
        return "identity"
    if connection.execute(
        "SELECT 1 FROM historical_job_links WHERE client_id=? AND normalized_url=? LIMIT 1",
        (client_id, str(job.canonical_url)),
    ).fetchone():
        return "url"
    return None


def _historical_kind(repository: SQLiteRepository, job: Any, client_id: str) -> str | None:
    with repository.connect() as connection:
        return _historical_kind_connection(connection, job, client_id)


def _delivery_evidence(job: Any, match: Any) -> dict[str, Any]:
    return {
        "company": job.company,
        "title": job.title,
        "location": job.location_text,
        "remote_status": job.remote_status.value,
        "source": job.source,
        "url": select_preferred_url(job),
        "decision": match.decision.value,
        "matched_reasons": match.matched_reasons,
        "rejection_reasons": match.rejection_reasons,
        "csv_row": export_row(job),
    }


def _completed_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not _has_runtime_schema():
        return {}
    with _runtime_connection() as connection:
        rows = connection.execute(
            "SELECT target_identity,result_json FROM stage_b_target_runs WHERE manifest_sha256=? AND state='completed' ORDER BY completed_at,target_identity",
            (manifest["manifest_sha256"],),
        ).fetchall()
    values = {}
    for row in rows:
        if not row["result_json"]:
            raise ValueError("completed Stage B target has no result evidence")
        value = json.loads(row["result_json"])
        if value.get("target_identity") != row["target_identity"]:
            raise ValueError("Stage B target result identity mismatch")
        values[row["target_identity"]] = value
    return values


def completed_values(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    values = _completed_map(manifest)
    return [
        values[target["target_identity"]]
        for target in manifest["targets"]
        if target["target_identity"] in values
    ]


def _pending(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    done = _completed_map(manifest)
    return [target for target in manifest["targets"] if target["target_identity"] not in done]


def select_invocation(
    manifest: dict[str, Any],
    *,
    max_targets: int,
    max_estimated_requests: int,
    include_high_cost: bool,
    include_potentially_capped: bool,
    allow_uncertain_cost: bool,
) -> tuple[list[dict[str, Any]], str | None]:
    if not 1 <= max_targets <= 100:
        raise ValueError("max targets must be between 1 and 100")
    if max_estimated_requests < 1:
        raise ValueError("max estimated requests must be positive")
    selected = []
    estimate = 0
    for target in _pending(manifest):
        if target["cost_tier"] == "exact_500_plus" and not include_high_cost:
            return selected, "awaiting_high_cost_opt_in"
        if target["cost_tier"] == "potentially_capped" and not (
            include_potentially_capped and allow_uncertain_cost
        ):
            return selected, "awaiting_potentially_capped_opt_in"
        cost = target["estimated_total_requests"]
        if cost is not None and estimate + cost > max_estimated_requests:
            return selected, "invocation_request_budget_reached"
        selected.append(target)
        estimate += cost or 0
        if len(selected) == max_targets:
            return selected, "invocation_target_limit_reached"
    return selected, None


def _begin_target(manifest: dict[str, Any], target: dict[str, Any]) -> bool:
    with _runtime_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT state FROM stage_b_target_runs WHERE manifest_sha256=? AND target_identity=?",
            (manifest["manifest_sha256"], target["target_identity"]),
        ).fetchone()
        if row and row["state"] == "completed":
            return False
        connection.execute(
            "INSERT INTO stage_b_target_runs(manifest_sha256,target_identity,state,started_at) VALUES(?,?,'in_progress',?) ON CONFLICT(manifest_sha256,target_identity) DO UPDATE SET state='in_progress',collection_status=NULL,completed_at=NULL,result_json=NULL,started_at=excluded.started_at",
            (manifest["manifest_sha256"], target["target_identity"], datetime.now(UTC).isoformat()),
        )
    return True


def _select_fresh_in_transaction(
    connection: sqlite3.Connection, jobs: list[Any], client_id: str
) -> tuple[list[Any], int, int, int]:
    representatives = {}
    identity = url = already = 0
    for job in sorted(jobs, key=representative_key):
        row = connection.execute(
            "SELECT group_id FROM posting_delivery_groups WHERE job_id=?", (job.id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"posting has no delivery group: {job.id}")
        group = row[0]
        historical = _historical_kind_connection(connection, job, client_id)
        if historical == "identity":
            identity += 1
            continue
        if historical == "url":
            url += 1
            continue
        if connection.execute(
            "SELECT 1 FROM group_deliveries WHERE group_id=? AND client_id=? AND destination=?",
            (group, client_id, CUMULATIVE_DESTINATION),
        ).fetchone():
            already += 1
            continue
        representatives.setdefault(group, job)
    return [representatives[group] for group in sorted(representatives)], identity, url, already


def _commit_target_completion(
    *,
    manifest: dict[str, Any],
    target: dict[str, Any],
    brief: SearchBrief,
    result: dict[str, Any],
    candidates: list[Any],
    matches: dict[str, Any],
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    with _runtime_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT state FROM stage_b_target_runs WHERE manifest_sha256=? AND target_identity=?",
            (manifest["manifest_sha256"], target["target_identity"]),
        ).fetchone()
        if row is None or row["state"] != "in_progress":
            raise ValueError("Stage B target completion requires durable in-progress state")
        fresh, identity, url, already = _select_fresh_in_transaction(
            connection, candidates, brief.client_id
        )
        deliveries = []
        for job in fresh:
            group = connection.execute(
                "SELECT group_id FROM posting_delivery_groups WHERE job_id=?", (job.id,)
            ).fetchone()[0]
            evidence = _delivery_evidence(job, matches[job.id])
            connection.execute(
                "INSERT INTO group_deliveries(group_id,client_id,destination,job_id,exported_at) VALUES(?,?,?,?,?)",
                (group, brief.client_id, CUMULATIVE_DESTINATION, job.id, now),
            )
            connection.execute(
                "INSERT OR IGNORE INTO exports(job_id,client_id,destination,exported_at) VALUES(?,?,?,?)",
                (job.id, brief.client_id, CUMULATIVE_DESTINATION, now),
            )
            connection.execute(
                "INSERT INTO stage_b_deliveries(manifest_sha256,target_identity,group_id,job_id,decision,evidence_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    manifest["manifest_sha256"],
                    target["target_identity"],
                    group,
                    job.id,
                    matches[job.id].decision.value,
                    canonical(evidence),
                    now,
                ),
            )
            deliveries.append(evidence)
        result = {
            **result,
            "state": "completed",
            "completed_at": now,
            "historical_identity": identity,
            "historical_url": url,
            "already_delivered_groups": already,
            "fresh_unique_deliveries": len(fresh),
            "deliveries": deliveries,
        }
        connection.execute(
            "UPDATE stage_b_target_runs SET state='completed',collection_status=?,completed_at=?,result_json=? WHERE manifest_sha256=? AND target_identity=?",
            (
                result["collection_status"],
                now,
                canonical(result),
                manifest["manifest_sha256"],
                target["target_identity"],
            ),
        )
    return result


def execute_target(
    *,
    target: dict[str, Any],
    manifest: dict[str, Any],
    repository: SQLiteRepository,
    brief: SearchBrief,
    delay: float,
) -> dict[str, Any]:
    collector, counter = _collector(delay)
    try:
        collected = collector.collect(_source_target(target))
        jobs = collected.jobs
        status = collected.status.value
        errors = collected.errors
        counts = dict(collector.last_counts)
        quarantined = len(errors) if collected.status is CollectionStatus.PARTIAL else 0
    except Exception as exc:  # noqa: BLE001 - target failure is durable and must not abort replay.
        jobs = []
        status = "runner_failure"
        errors = [f"{type(exc).__name__}: {exc}"]
        counts = {}
        quarantined = 0
    finally:
        calls = counter.client_request_calls
        counter.close()
    lifecycle = Counter()
    decisions = Counter()
    candidates = []
    matches = {}
    for job in jobs:
        lifecycle[repository.upsert_job(job).value] += 1
        match = match_job(job, brief)
        repository.save_match(match)
        decisions[match.decision.value] += 1
        if match.decision in {MatchDecision.STRONG_MATCH, MatchDecision.POSSIBLE_MATCH}:
            candidates.append(job)
            matches[job.id] = match
    nonhistorical = [
        job for job in candidates if _historical_kind(repository, job, brief.client_id) is None
    ]
    groups = {repository.delivery_group_id(job.id) for job in nonhistorical}
    return _commit_target_completion(
        manifest=manifest,
        target=target,
        brief=brief,
        candidates=candidates,
        matches=matches,
        result={
            "manifest_sha256": manifest["manifest_sha256"],
            "source": "workday",
            "target_identity": target["target_identity"],
            "collection_status": status,
            "errors": errors,
            "received": len(jobs),
            "quarantined": quarantined,
            "lifecycle": dict(lifecycle),
            "matcher_decisions": dict(decisions),
            "delivery_eligible_matched": len(candidates),
            "nonhistorical_matched": len(nonhistorical),
            "practical_duplicate_groups_collapsed": max(0, len(nonhistorical) - len(groups)),
            "client_request_calls": calls,
            "transport_retry_attempts": "unknown",
            "collector_counts": counts,
        },
    )


def regenerate_csv(manifest: dict[str, Any]) -> int:
    """Safely replace the CSV projection from durable delivery evidence."""
    if not _has_runtime_schema():
        INCREMENTAL_DELIVERIES.unlink(missing_ok=True)
        return 0
    with _runtime_connection() as connection:
        rows = connection.execute(
            "SELECT evidence_json FROM stage_b_deliveries WHERE manifest_sha256=? ORDER BY target_identity,group_id",
            (manifest["manifest_sha256"],),
        ).fetchall()
    import csv

    INCREMENTAL_DELIVERIES.parent.mkdir(parents=True, exist_ok=True)
    with INCREMENTAL_DELIVERIES.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            csv_row = json.loads(row["evidence_json"]).get("csv_row")
            if not isinstance(csv_row, dict) or set(csv_row) != set(CSV_COLUMNS):
                raise ValueError("Stage B delivery evidence cannot deterministically project CSV")
            writer.writerow({column: csv_row[column] for column in CSV_COLUMNS})
    return len(rows)


def _delivery_totals(manifest: dict[str, Any]) -> tuple[int, int, int]:
    """Return capacity counts only from durable Stage B delivery rows."""
    if not _has_runtime_schema():
        return 0, 0, 0
    with _runtime_connection() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS fresh, "
            "SUM(decision=?) AS strong, SUM(decision=?) AS review "
            "FROM stage_b_deliveries WHERE manifest_sha256=?",
            (
                MatchDecision.STRONG_MATCH.value,
                MatchDecision.POSSIBLE_MATCH.value,
                manifest["manifest_sha256"],
            ),
        ).fetchone()
    return int(row["fresh"] or 0), int(row["strong"] or 0), int(row["review"] or 0)


def _systemic_streak(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    streak = []
    for value in sorted(values, key=lambda item: item.get("completed_at", "")):
        streak = streak + [value] if value.get("collection_status") in SYSTEMIC_FAILURES else []
    return streak


def summarize(
    manifest: dict[str, Any], values: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    values = completed_values(manifest) if values is None else values
    statuses = Counter(value.get("collection_status") for value in values)
    lifecycle = Counter()
    decisions = Counter()
    coverage = Counter()
    for value in values:
        lifecycle.update(value.get("lifecycle", {}))
        decisions.update(value.get("matcher_decisions", {}))
        if isinstance(value.get("collector_counts", {}).get("coverage_mode"), str):
            coverage[value["collector_counts"]["coverage_mode"]] += 1
    pending = _pending(manifest)
    fresh, strong, review = _delivery_totals(manifest)
    combined = STAGE_A_BASELINE_FRESH + fresh
    completed = len(values)
    stop = (
        "combined_preferred_threshold_reached"
        if combined >= PREFERRED_THRESHOLD
        else "all_stage_b_targets_completed"
        if completed == len(manifest["targets"])
        else None
    )
    return {
        "manifest_sha256": manifest["manifest_sha256"],
        "updated_at": datetime.now(UTC).isoformat(),
        "targets": {
            "completed": completed,
            "remaining": len(manifest["targets"]) - completed,
            "success": statuses[CollectionStatus.SUCCESS.value],
            "partial": statuses[CollectionStatus.PARTIAL.value],
            "failed": completed
            - statuses[CollectionStatus.SUCCESS.value]
            - statuses[CollectionStatus.PARTIAL.value],
            "pending_deferred_high_cost": sum(t["cost_tier"] == "exact_500_plus" for t in pending),
            "pending_deferred_capped": sum(t["cost_tier"] == "potentially_capped" for t in pending),
        },
        "collection": {
            "provider_reported_broad_total_sum": sum(
                int(v.get("collector_counts", {}).get("broad_total", 0) or 0) for v in values
            ),
            "paths_discovered": sum(
                int(v.get("collector_counts", {}).get("paths_discovered", 0) or 0) for v in values
            ),
            "detail_attempts": sum(
                int(v.get("collector_counts", {}).get("detail_attempts", 0) or 0) for v in values
            ),
            "normalized_jobs": sum(v.get("received", 0) for v in values),
            "errors": sum(len(v.get("errors", [])) for v in values),
            "quarantined": sum(v.get("quarantined", 0) for v in values),
            "client_request_calls": sum(v.get("client_request_calls", 0) for v in values),
            "transport_retry_attempts": "unknown",
            "frozen_estimated_requests": sum(
                t["estimated_total_requests"] or 0
                for t in manifest["targets"]
                if any(v["target_identity"] == t["target_identity"] for v in values)
            ),
            "coverage_modes": dict(coverage),
            "partition_count": sum(
                int(v.get("collector_counts", {}).get("partition_count", 0) or 0) for v in values
            ),
            **dict(lifecycle),
        },
        "matching": dict(decisions),
        "historical_suppression": {
            "exact_source_identity": sum(v.get("historical_identity", 0) for v in values),
            "normalized_url_fallback": sum(v.get("historical_url", 0) for v in values),
            "total": sum(
                v.get("historical_identity", 0) + v.get("historical_url", 0) for v in values
            ),
        },
        "delivery": {
            "matched_before_delivery_dedupe": sum(
                v.get("delivery_eligible_matched", 0) for v in values
            ),
            "non_historical_matched": sum(v.get("nonhistorical_matched", 0) for v in values),
            "already_delivered_groups": sum(v.get("already_delivered_groups", 0) for v in values),
            "practical_duplicate_groups_collapsed": sum(
                v.get("practical_duplicate_groups_collapsed", 0) for v in values
            ),
            "stage_b_incremental_fresh": fresh,
            "stage_b_incremental_strong": strong,
            "stage_b_incremental_review": review,
        },
        "capacity": {
            "stage_a_baseline": STAGE_A_BASELINE_FRESH,
            "stage_b_incremental_fresh": fresh,
            "combined_fresh": combined,
            "business_threshold": BUSINESS_THRESHOLD,
            "business_threshold_reached": combined >= BUSINESS_THRESHOLD,
            "remaining_to_business_threshold": max(0, BUSINESS_THRESHOLD - combined),
            "preferred_threshold": PREFERRED_THRESHOLD,
            "preferred_threshold_reached": combined >= PREFERRED_THRESHOLD,
            "remaining_to_preferred_threshold": max(0, PREFERRED_THRESHOLD - combined),
        },
        "terminal_stop_reason": stop,
    }


def report(summary: dict[str, Any]) -> str:
    capacity = summary["capacity"]
    result = (
        "Stage B has not collected any target. This is protocol/preflight evidence, not a capacity result."
        if summary["terminal_stop_reason"] is None and summary["targets"]["completed"] == 0
        else "Stage B is incomplete. An invocation pause is not a final capacity verdict."
        if summary["terminal_stop_reason"] is None
        else f"Terminal stop reason: `{summary['terminal_stop_reason']}`."
    )
    return "\n".join(
        [
            "# Taiwo Sourcing Capacity Replay V1 — Stage B",
            "",
            f"Manifest: `{summary['manifest_sha256']}`",
            "",
            result,
            "",
            "## Capacity state",
            "",
            f"- Frozen Stage A baseline: {capacity['stage_a_baseline']}",
            f"- Stage B incremental fresh: {capacity['stage_b_incremental_fresh']}",
            f"- Combined fresh: {capacity['combined_fresh']}",
            f"- Business threshold: {capacity['business_threshold']}",
            f"- Preferred threshold: {capacity['preferred_threshold']}",
            "",
            "Provider totals, discovered paths, normalized postings, matching outcomes, and fresh delivery groups are separate measures.",
            "",
        ]
    )


def write_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    summary = summarize(manifest)
    write_json(SUMMARY_PATH, summary)
    REPORT_PATH.write_text(report(summary), encoding="utf-8")
    return summary


def _latest_invocation(manifest: dict[str, Any]) -> dict[str, Any] | None:
    if not _has_runtime_schema():
        return None
    with _runtime_connection() as connection:
        row = connection.execute(
            "SELECT * FROM stage_b_invocations WHERE manifest_sha256=? ORDER BY started_at DESC LIMIT 1",
            (manifest["manifest_sha256"],),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "pause_reason": row["pause_reason"],
        "terminal_stop_reason": row["terminal_stop_reason"],
        "attempted_targets": json.loads(row["attempted_targets_json"]),
        "systemic_evidence": json.loads(row["systemic_evidence_json"]),
    }


def preflight(
    manifest: dict[str, Any],
    *,
    max_targets: int,
    max_estimated_requests: int,
    include_high_cost: bool,
    include_potentially_capped: bool,
    allow_uncertain_cost: bool,
) -> dict[str, Any]:
    summary = summarize(manifest)
    selected, planned = select_invocation(
        manifest,
        max_targets=max_targets,
        max_estimated_requests=max_estimated_requests,
        include_high_cost=include_high_cost,
        include_potentially_capped=include_potentially_capped,
        allow_uncertain_cost=allow_uncertain_cost,
    )
    current = _latest_invocation(manifest)
    return {
        "network_work": False,
        "manifest_sha256": manifest["manifest_sha256"],
        "remaining_targets": summary["targets"]["remaining"],
        "stage_b_incremental_fresh": summary["capacity"]["stage_b_incremental_fresh"],
        "combined_fresh": summary["capacity"]["combined_fresh"],
        "selected_targets": [
            {
                key: t[key]
                for key in (
                    "target_identity",
                    "cost_tier",
                    "estimated_list_requests",
                    "estimated_detail_requests",
                    "estimated_total_requests",
                    "potentially_capped",
                )
            }
            for t in selected
        ],
        "total_frozen_invocation_estimate": sum(
            t["estimated_total_requests"] or 0 for t in selected
        ),
        "current_invocation_pause_reason": current["pause_reason"] if current else None,
        "planned_pause_reason": planned,
        "terminal_stop_reason": summary["terminal_stop_reason"],
        "latest_invocation": current,
    }


def _begin_invocation(manifest: dict[str, Any], config: dict[str, Any]) -> str:
    import uuid

    identity = str(uuid.uuid4())
    with _runtime_connection() as connection:
        connection.execute(
            "INSERT INTO stage_b_invocations(id,manifest_sha256,started_at,config_json,attempted_targets_json,systemic_evidence_json) VALUES(?,?,?,?,'[]','[]')",
            (
                identity,
                manifest["manifest_sha256"],
                datetime.now(UTC).isoformat(),
                canonical(config),
            ),
        )
    return identity


def _record_attempt(invocation: str, target: dict[str, Any]) -> None:
    with _runtime_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT attempted_targets_json FROM stage_b_invocations WHERE id=?", (invocation,)
        ).fetchone()
        attempts = json.loads(row[0])
        attempts.append(target["target_identity"])
        connection.execute(
            "UPDATE stage_b_invocations SET attempted_targets_json=? WHERE id=?",
            (canonical(attempts), invocation),
        )


def _finish_invocation(
    invocation: str,
    *,
    pause_reason: str | None,
    terminal_stop_reason: str | None,
    systemic_evidence: list[dict[str, str]],
) -> None:
    with _runtime_connection() as connection:
        connection.execute(
            "UPDATE stage_b_invocations SET completed_at=?,pause_reason=?,terminal_stop_reason=?,systemic_evidence_json=? WHERE id=?",
            (
                datetime.now(UTC).isoformat(),
                pause_reason,
                terminal_stop_reason,
                canonical(systemic_evidence),
                invocation,
            ),
        )


def _lock_is_active(path: Path) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        os.kill(value["pid"], 0)
    except (KeyError, OSError, TypeError, ValueError):
        return False
    return True


@contextlib.contextmanager
def run_lock() -> Any:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if _lock_is_active(LOCK):
            raise RuntimeError("a Stage B replay invocation is already running")
        LOCK.unlink(missing_ok=True)
        descriptor = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(
            descriptor,
            canonical({"pid": os.getpid(), "started_at": datetime.now(UTC).isoformat()}).encode(),
        )
        yield
    finally:
        os.close(descriptor)
        LOCK.unlink(missing_ok=True)


def run(
    manifest: dict[str, Any],
    *,
    max_targets: int,
    max_estimated_requests: int,
    delay: float,
    include_high_cost: bool,
    include_potentially_capped: bool,
    allow_uncertain_cost: bool,
) -> dict[str, Any]:
    if delay < MIN_DELAY:
        raise ValueError(f"delay must be at least {MIN_DELAY} seconds")
    _brief()
    with run_lock():
        bootstrap(manifest)
        repository = SQLiteRepository(DATABASE)
        selected, planned = select_invocation(
            manifest,
            max_targets=max_targets,
            max_estimated_requests=max_estimated_requests,
            include_high_cost=include_high_cost,
            include_potentially_capped=include_potentially_capped,
            allow_uncertain_cost=allow_uncertain_cost,
        )
        invocation = _begin_invocation(
            manifest,
            {
                "max_targets": max_targets,
                "max_estimated_requests": max_estimated_requests,
                "delay": delay,
                "include_high_cost": include_high_cost,
                "include_potentially_capped": include_potentially_capped,
                "allow_uncertain_cost": allow_uncertain_cost,
            },
        )
        for target in selected:
            _record_attempt(invocation, target)
            if not _begin_target(manifest, target):
                continue
            execute_target(
                target=target, manifest=manifest, repository=repository, brief=_brief(), delay=delay
            )
            regenerate_csv(manifest)
            summary = write_summary(manifest)
            if summary["terminal_stop_reason"]:
                _finish_invocation(
                    invocation,
                    pause_reason=None,
                    terminal_stop_reason=summary["terminal_stop_reason"],
                    systemic_evidence=[],
                )
                return {**summary, "invocation_pause_reason": None}
            streak = _systemic_streak(completed_values(manifest))
            if len(streak) >= 3:
                evidence = [
                    {
                        "target_identity": v["target_identity"],
                        "collection_status": v["collection_status"],
                    }
                    for v in streak
                ]
                _finish_invocation(
                    invocation,
                    pause_reason="systemic_provider_failure_pause",
                    terminal_stop_reason=None,
                    systemic_evidence=evidence,
                )
                return {
                    **summary,
                    "invocation_pause_reason": "systemic_provider_failure_pause",
                    "systemic_failure_targets": [item["target_identity"] for item in evidence],
                }
            time.sleep(delay)
        regenerate_csv(manifest)
        summary = write_summary(manifest)
        _finish_invocation(
            invocation,
            pause_reason=planned,
            terminal_stop_reason=summary["terminal_stop_reason"],
            systemic_evidence=[],
        )
        return {**summary, "invocation_pause_reason": planned}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--freeze-manifest", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--max-targets", type=int, default=DEFAULT_MAX_TARGETS)
    parser.add_argument(
        "--max-estimated-requests", type=int, default=DEFAULT_MAX_ESTIMATED_REQUESTS
    )
    parser.add_argument("--delay", type=float, default=MIN_DELAY)
    parser.add_argument("--include-high-cost", action="store_true")
    parser.add_argument("--include-potentially-capped", action="store_true")
    parser.add_argument("--allow-uncertain-cost", action="store_true")
    args = parser.parse_args()
    if args.freeze_manifest:
        if args.resume or args.status:
            parser.error("--freeze-manifest cannot be combined with --resume or --status")
        print(json.dumps(freeze_manifest(), indent=2, sort_keys=True))
        return
    manifest = load_manifest()
    arguments = {
        "max_targets": args.max_targets,
        "max_estimated_requests": args.max_estimated_requests,
        "include_high_cost": args.include_high_cost,
        "include_potentially_capped": args.include_potentially_capped,
        "allow_uncertain_cost": args.allow_uncertain_cost,
    }
    output = (
        run(manifest, delay=args.delay, **arguments)
        if args.resume
        else preflight(manifest, **arguments)
    )
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
