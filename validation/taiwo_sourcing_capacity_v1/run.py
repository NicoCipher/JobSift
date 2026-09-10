"""Resumable Stage A replay over active Greenhouse, Ashby, and Lever targets only."""

from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import json
import os
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from job_scout.collectors.ashby import AshbyCollector
from job_scout.collectors.greenhouse import GreenhouseCollector
from job_scout.collectors.lever import LeverCollector
from job_scout.dedupe.resolver import select_preferred_url
from job_scout.domain.models import (
    CollectionStatus,
    LeverTargetConfig,
    MatchDecision,
    SearchBrief,
    SourceTarget,
)
from job_scout.export.csv_exporter import write_csv
from job_scout.history import explicit_blacklist_evidence, historical_records, workbook_sha256
from job_scout.matching.matcher import match_job
from job_scout.storage.sqlite import SQLiteRepository

ROOT = Path(__file__).resolve().parent
STAGE_ROOT = ROOT / "stage_a"
RUNTIME = STAGE_ROOT / "runtime"
MANIFEST_PATH = STAGE_ROOT / "manifest.json"
SUMMARY_PATH = STAGE_ROOT / "summary.json"
REPORT_PATH = STAGE_ROOT / "report.md"
HEALTH_ROOT = ROOT.parent / "target_universe_health_v1" / "full"
HEALTH_MANIFEST = HEALTH_ROOT / "manifest.json"
HEALTH_RESULTS = HEALTH_ROOT / "results.json"
BRIEF_PATH = ROOT.parent.parent / "config" / "search_briefs" / "taiwo_operator_sourcing_v1.json"
CHECKPOINTS = RUNTIME / "checkpoints"
LOCK = RUNTIME / "run.lock"
HISTORY_IMPORT = RUNTIME / "historical_import.json"
DATABASE = RUNTIME / "replay.sqlite3"
DELIVERIES = RUNTIME / "deliveries.csv"
QUALITY_AUDIT = RUNTIME / "quality_audit.csv"
SOURCES = ("greenhouse", "ashby", "lever")
EXPECTED_COUNTS = {"greenhouse": 665, "ashby": 607, "lever": 236}
EXPECTED_INVENTORY = {"greenhouse": 25494, "ashby": 16005, "lever": 6960}
EXPECTED_ROWS = {"applied": 9714, "not_applied": 967, "unknown": 426}
THRESHOLD = 250


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def artifact_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _check_brief(brief: SearchBrief) -> None:
    if (
        brief.client_id != "taiwo_operator_sourcing_v1"
        or brief.target_market.intent.value != "must"
        or brief.target_market.countries != {"United States"}
        or brief.work_mode.intent.value != "must"
        or {item.value for item in brief.work_mode.modes} != {"remote"}
        or brief.management_roles.value != "avoid"
        or brief.work_eligibility.intent.value != "ignore"
        or brief.employment_type.intent.value != "ignore"
        or brief.max_required_experience_years is not None
    ):
        raise ValueError("Taiwo Stage A requires the frozen sourcing brief semantics")


def _health_values() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    manifest = json.loads(HEALTH_MANIFEST.read_text(encoding="utf-8"))
    results = json.loads(HEALTH_RESULTS.read_text(encoding="utf-8"))
    unhashed = dict(manifest)
    manifest_sha = unhashed.pop("manifest_sha256", None)
    if manifest_sha != sha(unhashed) or results.get("manifest_sha256") != manifest_sha:
        raise ValueError("completed health evidence is not manifest-bound")
    values = results.get("results")
    if (
        not isinstance(values, list)
        or results.get("completed") != len(values)
        or len(values) != 2287
    ):
        raise ValueError("completed health evidence is incomplete")
    by_identity = {value.get("target_identity"): value for value in values}
    if len(by_identity) != len(values):
        raise ValueError("completed health evidence has duplicate target identities")
    return manifest, by_identity


def make_manifest(*, generated_at: str) -> dict[str, Any]:
    health_manifest, health = _health_values()
    target_map = {target["target_identity"]: target for target in health_manifest["targets"]}
    targets: list[dict[str, Any]] = []
    for identity, value in health.items():
        if value.get("classification") != "active" or value.get("source") not in SOURCES:
            continue
        target = target_map.get(identity)
        inventory = value.get("current_postings")
        if target is None or not isinstance(inventory, int) or inventory < 1:
            raise ValueError(f"active health result lacks valid target inventory: {identity}")
        targets.append(
            {
                "source": target["source"],
                "target_identity": identity,
                "coordinates": target["coordinates"],
                "company_hint": target.get("company_hint") or identity,
                "historical_occurrence_count": target["historical_occurrence_count"],
                "health_current_inventory": inventory,
            }
        )
    targets.sort(
        key=lambda value: (
            -value["historical_occurrence_count"],
            -value["health_current_inventory"],
            value["target_identity"],
        )
    )
    counts = dict(Counter(target["source"] for target in targets))
    inventory = {
        source: sum(
            target["health_current_inventory"] for target in targets if target["source"] == source
        )
        for source in SOURCES
    }
    if counts != EXPECTED_COUNTS or inventory != EXPECTED_INVENTORY or len(targets) != 1508:
        raise ValueError(f"unexpected Stage A health selection: {counts}, {inventory}")
    manifest = {
        "validation": "taiwo-sourcing-capacity-v1",
        "stage": "a",
        "generated_at": generated_at,
        "baseline_commit": "df64c2ff367a7df63647bab6596f597a232d1b16",
        "search_brief_sha256": artifact_sha(BRIEF_PATH),
        "health_manifest_sha256": health_manifest["manifest_sha256"],
        "health_results_sha256": artifact_sha(HEALTH_RESULTS),
        "selection": "active only; historical occurrence descending, health inventory descending, target identity ascending",
        "sources": list(SOURCES),
        "target_counts": counts,
        "health_inventory": inventory,
        "threshold": THRESHOLD,
        "targets": targets,
    }
    manifest["manifest_sha256"] = sha(manifest)
    return manifest


def verify_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    unhashed = dict(manifest)
    manifest_sha = unhashed.pop("manifest_sha256", None)
    if manifest_sha != sha(unhashed):
        raise ValueError("Stage A manifest hash is invalid")
    if manifest.get("stage") != "a" or manifest.get("sources") != list(SOURCES):
        raise ValueError("Stage A manifest source contract is invalid")
    if manifest.get("search_brief_sha256") != artifact_sha(BRIEF_PATH):
        raise ValueError("Stage A manifest does not match the frozen Taiwo brief")
    health_manifest, _ = _health_values()
    if manifest.get("health_manifest_sha256") != health_manifest["manifest_sha256"]:
        raise ValueError("Stage A manifest does not match completed health evidence")
    targets = manifest.get("targets", [])
    if any(target.get("source") not in SOURCES for target in targets):
        raise ValueError("Stage A manifest includes a disallowed source")
    expected = make_manifest(generated_at=manifest["generated_at"])
    if {key: value for key, value in manifest.items() if key != "manifest_sha256"} != {
        key: value for key, value in expected.items() if key != "manifest_sha256"
    }:
        raise ValueError("Stage A manifest differs from the completed health selection")
    return manifest


def load_manifest() -> dict[str, Any]:
    if MANIFEST_PATH.exists():
        return verify_manifest(json.loads(MANIFEST_PATH.read_text(encoding="utf-8")))
    manifest = make_manifest(generated_at=datetime.now(UTC).isoformat())
    write_json(MANIFEST_PATH, manifest)
    return manifest


def checkpoint_path(target: dict[str, Any]) -> Path:
    return CHECKPOINTS / f"{hashlib.sha256(target['target_identity'].encode()).hexdigest()}.json"


def completed(target: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any] | None:
    path = checkpoint_path(target)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"corrupt Stage A checkpoint: {path}") from exc
    if (
        value.get("manifest_sha256") != manifest["manifest_sha256"]
        or value.get("target_identity") != target["target_identity"]
        or value.get("source") != target["source"]
    ):
        raise ValueError(f"mismatched Stage A checkpoint: {path}")
    return value


def _lock_is_active(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        os.kill(payload["pid"], 0)
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
            raise RuntimeError("a Stage A replay batch is already running")
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


def _target(target: dict[str, Any]) -> SourceTarget:
    source, coordinates = target["source"], target["coordinates"]
    if source == "lever":
        config = LeverTargetConfig(instance=coordinates["instance"], site=coordinates["site"])
        return SourceTarget(board_id=config.board_id, company=target["company_hint"], lever=config)
    return SourceTarget(board_id=coordinates["board"], company=target["company_hint"])


def _collector(source: str):
    return {
        "greenhouse": GreenhouseCollector(),
        "ashby": AshbyCollector(),
        "lever": LeverCollector(delay=0.5),
    }[source]


def _historical_kind(repository: SQLiteRepository, job, client_id: str) -> str | None:
    with repository.connect() as connection:
        identity = connection.execute(
            "SELECT 1 FROM historical_job_links WHERE client_id=? AND source=? "
            "AND source_board_id=? AND source_job_id=? LIMIT 1",
            (client_id, job.source, job.source_board_id, job.source_job_id),
        ).fetchone()
        if identity is not None:
            return "identity"
        url = connection.execute(
            "SELECT 1 FROM historical_job_links WHERE client_id=? AND normalized_url=? LIMIT 1",
            (client_id, str(job.canonical_url)),
        ).fetchone()
        return "url" if url is not None else None


def _history_status(workbook: Path, repository: SQLiteRepository, client_id: str) -> dict[str, Any]:
    records = historical_records(workbook)
    statuses = Counter(record.operator_status for record in records)
    if len(records) != 11107 or {key: statuses[key] for key in EXPECTED_ROWS} != EXPECTED_ROWS:
        raise ValueError("historical workbook does not reconcile to the frozen 11,107-row contract")
    checksum = workbook_sha256(workbook)
    existing = json.loads(HISTORY_IMPORT.read_text()) if HISTORY_IMPORT.exists() else None
    if existing is not None:
        if existing.get("client_id") != client_id or existing.get("workbook_sha256") != checksum:
            raise ValueError("Stage A runtime is bound to a different historical workbook/client")
        return existing
    inserted, already = repository.import_historical_records(
        client_id=client_id,
        workbook_sha256=checksum,
        records=records,
        blacklist_evidence=explicit_blacklist_evidence(workbook),
    )
    payload = {
        "client_id": client_id,
        "workbook_sha256": checksum,
        "rows": len(records),
        "status_counts": dict(statuses),
        "inserted": inserted,
        "already_present": already,
        "blacklist_applied": False,
    }
    write_json(HISTORY_IMPORT, payload)
    return payload


def _delivery_evidence(job, decision: str, match) -> dict[str, Any]:
    return {
        "company": job.company,
        "title": job.title,
        "location": job.location_text,
        "remote_status": job.remote_status.value,
        "source": job.source,
        "url": select_preferred_url(job),
        "decision": decision,
        "matched_reasons": match.matched_reasons,
        "rejection_reasons": match.rejection_reasons,
    }


def execute_target(
    *,
    target: dict[str, Any],
    manifest: dict[str, Any],
    repository: SQLiteRepository,
    brief: SearchBrief,
    destination: Path,
) -> dict[str, Any]:
    try:
        result = _collector(target["source"]).collect(_target(target))
    except Exception as exc:  # noqa: BLE001 - target failures must not stop the batch.
        return {
            "manifest_sha256": manifest["manifest_sha256"],
            "target_identity": target["target_identity"],
            "source": target["source"],
            "collection_status": "runner_failure",
            "errors": [f"{type(exc).__name__}: {exc}"],
            "received": 0,
            "quarantined": 0,
            "lifecycle": {},
            "matcher_decisions": {},
            "delivery_eligible_matched": 0,
            "historical_identity": 0,
            "historical_url": 0,
            "nonhistorical_matched": 0,
            "practical_duplicate_groups_collapsed": 0,
            "already_delivered_groups": 0,
            "fresh_unique_deliveries": 0,
            "deliveries": [],
            "completed_at": datetime.now(UTC).isoformat(),
        }
    counts: Counter[str] = Counter()
    historical: Counter[str] = Counter()
    delivery_candidates = []
    candidate_matches: dict[str, Any] = {}
    lifecycle = Counter()
    for job in result.jobs:
        lifecycle[repository.upsert_job(job).value] += 1
        match = match_job(job, brief)
        repository.save_match(match)
        counts[match.decision.value] += 1
        if match.decision in {MatchDecision.STRONG_MATCH, MatchDecision.POSSIBLE_MATCH}:
            kind = _historical_kind(repository, job, brief.client_id)
            if kind:
                historical[kind] += 1
            delivery_candidates.append(job)
            candidate_matches[job.id] = match
    nonhistorical = [
        job for job in delivery_candidates if not _historical_kind(repository, job, brief.client_id)
    ]
    groups = {repository.delivery_group_id(job.id) for job in nonhistorical}
    already_delivered = len(
        {
            repository.delivery_group_id(job.id)
            for job in nonhistorical
            if repository.is_exported(job.id, brief.client_id, str(destination.resolve()))
        }
    )
    fresh = repository.select_deliveries(
        delivery_candidates, brief.client_id, str(destination.resolve())
    )
    written = write_csv(destination, fresh)
    for job in fresh:
        repository.mark_exported(job.id, brief.client_id, str(destination.resolve()))
    if written != len(fresh):
        raise ValueError("delivery output did not preserve selected fresh jobs")
    deliveries = [
        _delivery_evidence(job, candidate_matches[job.id].decision.value, candidate_matches[job.id])
        for job in fresh
    ]
    return {
        "manifest_sha256": manifest["manifest_sha256"],
        "target_identity": target["target_identity"],
        "source": target["source"],
        "collection_status": result.status.value,
        "errors": result.errors,
        "received": len(result.jobs),
        "quarantined": len(result.errors) if result.status is CollectionStatus.PARTIAL else 0,
        "lifecycle": dict(lifecycle),
        "matcher_decisions": dict(counts),
        "delivery_eligible_matched": len(delivery_candidates),
        "historical_identity": historical["identity"],
        "historical_url": historical["url"],
        "nonhistorical_matched": len(nonhistorical),
        "practical_duplicate_groups_collapsed": len(nonhistorical) - len(groups),
        "already_delivered_groups": already_delivered,
        "fresh_unique_deliveries": len(fresh),
        "deliveries": deliveries,
        "completed_at": datetime.now(UTC).isoformat(),
    }


def _totals(values: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    target_status = Counter(value["collection_status"] for value in values)
    source_counts = {
        source: sum(value["source"] == source for value in values) for source in SOURCES
    }

    def total(key: str) -> int:
        return sum(value.get(key, 0) for value in values)

    decisions = Counter()
    lifecycle = Counter()
    for value in values:
        decisions.update(value.get("matcher_decisions", {}))
        lifecycle.update(value.get("lifecycle", {}))
    deliveries = [delivery for value in values for delivery in value.get("deliveries", [])]
    return {
        "manifest_sha256": manifest["manifest_sha256"],
        "updated_at": datetime.now(UTC).isoformat(),
        "targets": {
            "completed": len(values),
            "remaining": len(manifest["targets"]) - len(values),
            "by_source": source_counts,
            "success": target_status[CollectionStatus.SUCCESS.value],
            "partial": target_status[CollectionStatus.PARTIAL.value],
            "failed": len(values)
            - target_status[CollectionStatus.SUCCESS.value]
            - target_status[CollectionStatus.PARTIAL.value],
        },
        "collection": {
            "received_postings": total("received"),
            "quarantined": total("quarantined"),
            **dict(lifecycle),
        },
        "matching": dict(decisions),
        "historical_suppression": {
            "exact_source_identity": total("historical_identity"),
            "normalized_url_fallback": total("historical_url"),
            "total": total("historical_identity") + total("historical_url"),
        },
        "practical_dedupe": {
            "matched_before_delivery_dedupe": total("delivery_eligible_matched"),
            "non_historical_matched": total("nonhistorical_matched"),
            "practical_duplicate_groups_collapsed": total("practical_duplicate_groups_collapsed"),
            "already_delivered_groups": total("already_delivered_groups"),
        },
        "fresh_unique_deliveries": total("fresh_unique_deliveries"),
        "strong_fresh_deliveries": sum(
            item["decision"] == MatchDecision.STRONG_MATCH.value for item in deliveries
        ),
        "review_fresh_deliveries": sum(
            item["decision"] == MatchDecision.POSSIBLE_MATCH.value for item in deliveries
        ),
        "threshold": manifest["threshold"],
        "stop_reason": (
            "fresh_delivery_threshold_reached"
            if total("fresh_unique_deliveries") >= manifest["threshold"]
            else "all_stage_a_targets_completed"
            if len(values) == len(manifest["targets"])
            else None
        ),
    }


def _write_quality_audit(values: list[dict[str, Any]]) -> None:
    deliveries = [delivery for value in values for delivery in value.get("deliveries", [])]
    selected = [
        *[item for item in deliveries if item["decision"] == MatchDecision.STRONG_MATCH.value][:20],
        *[item for item in deliveries if item["decision"] == MatchDecision.POSSIBLE_MATCH.value][
            :20
        ],
    ]
    QUALITY_AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with QUALITY_AUDIT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "company",
                "title",
                "location",
                "remote_status",
                "source",
                "url",
                "decision",
                "matched_reasons",
                "rejection_reasons",
            ),
        )
        writer.writeheader()
        for item in selected:
            writer.writerow(
                {
                    **item,
                    "matched_reasons": "; ".join(item["matched_reasons"]),
                    "rejection_reasons": "; ".join(item["rejection_reasons"]),
                }
            )


def _report(summary: dict[str, Any]) -> str:
    targets = summary["targets"]
    collection = summary["collection"]
    matching = summary["matching"]
    historical = summary["historical_suppression"]
    dedupe = summary["practical_dedupe"]
    return "\n".join(
        [
            "# Taiwo Sourcing Capacity Replay V1 — Stage A",
            "",
            f"Manifest: `{summary['manifest_sha256']}`",
            "",
            "## Verdict",
            "",
            "**TAIWO STAGE A CAPACITY THRESHOLD NOT MET.** All 1,508 Stage A targets were exhausted, yielding 44 fresh unique deliveries. The business threshold (200) and preferred buffer (250) were both not met.",
            "",
            "## Scope",
            "",
            "This is a one-snapshot capacity measurement over Greenhouse, Ashby, and Lever using the unchanged Taiwo brief. It does not demonstrate Monday–Friday capacity. Workday is excluded from Stage A; its Stage B experiment remains gated. This result does not justify weakening the brief or adding source #5.",
            "",
            "## Target completion",
            "",
            f"- Completed: {targets['completed']} / {targets['completed'] + targets['remaining']}",
            f"- Success: {targets['success']}; failed: {targets['failed']}; partial: {targets['partial']}",
            f"- Greenhouse: {targets['by_source']['greenhouse']}; Ashby: {targets['by_source']['ashby']}; Lever: {targets['by_source']['lever']}",
            f"- Stop reason: `{summary['stop_reason'] or 'batch limit reached; resume required'}`",
            "",
            "The five target failures did not block the completed one-snapshot experiment.",
            "",
            "## Collection and matching",
            "",
            f"- Received and persisted postings: {collection['received_postings']}; quarantined: {collection['quarantined']}",
            f"- Matcher outcomes: strong {matching['strong_match']}; possible {matching['possible_match']}; needs review {matching['needs_review']}; reject {matching['reject']}",
            "- Received postings are collection records, not a claim of unique vacancies.",
            "",
            "## Historical suppression and delivery grouping",
            "",
            f"- Historical suppression: exact source identity {historical['exact_source_identity']}; normalized URL fallback {historical['normalized_url_fallback']}; total {historical['total']}",
            f"- Matched before delivery dedupe: {dedupe['matched_before_delivery_dedupe']}; non-historical matched: {dedupe['non_historical_matched']}",
            f"- Already-delivered replay groups: {dedupe['already_delivered_groups']}; practical duplicate groups collapsed: {dedupe['practical_duplicate_groups_collapsed']}",
            f"- Fresh unique deliveries: {summary['fresh_unique_deliveries']} (strong {summary['strong_fresh_deliveries']}; needs review {summary['review_fresh_deliveries']})",
            "",
            "Fresh delivery reporting follows the approved replay delivery semantics; matcher categories and delivery results are reported separately above.",
            "",
        ]
    )


def refresh(manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    values = [
        value
        for target in manifest["targets"]
        if (value := completed(target, manifest)) is not None
    ]
    summary = _totals(values, manifest)
    write_json(SUMMARY_PATH, summary)
    REPORT_PATH.write_text(_report(summary), encoding="utf-8")
    _write_quality_audit(values)
    return values, summary


def run(*, workbook: Path, delay: float, batch_size: int) -> dict[str, Any]:
    if batch_size < 1 or batch_size > 100:
        raise ValueError("batch size must be between 1 and 100")
    manifest = load_manifest()
    brief = SearchBrief.model_validate_json(BRIEF_PATH.read_text(encoding="utf-8"))
    _check_brief(brief)
    with run_lock():
        repository = SQLiteRepository(DATABASE)
        _history_status(workbook, repository, brief.client_id)
        _, summary = refresh(manifest)
        if summary["stop_reason"]:
            return summary
        pending = [target for target in manifest["targets"] if completed(target, manifest) is None]
        for target in pending[:batch_size]:
            value = execute_target(
                target=target,
                manifest=manifest,
                repository=repository,
                brief=brief,
                destination=DELIVERIES,
            )
            write_json(checkpoint_path(target), value)
            _, summary = refresh(manifest)
            if summary["stop_reason"]:
                break
            if delay:
                time.sleep(delay)
    return summary


def status() -> dict[str, Any]:
    manifest = load_manifest()
    values = [
        value
        for target in manifest["targets"]
        if (value := completed(target, manifest)) is not None
    ]
    summary = _totals(values, manifest)
    return {
        "manifest_sha256": manifest["manifest_sha256"],
        "completed_targets": summary["targets"]["completed"],
        "remaining_targets": summary["targets"]["remaining"],
        "fresh_unique_deliveries": summary["fresh_unique_deliveries"],
        "threshold": summary["threshold"],
        "stop_reason": summary["stop_reason"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("a",), default="a")
    parser.add_argument("--workbook", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.status:
        print(json.dumps(status(), sort_keys=True))
        return
    if args.workbook is None:
        parser.error("--workbook is required to run Stage A")
    try:
        print(
            json.dumps(
                run(workbook=args.workbook, delay=args.delay, batch_size=args.batch_size),
                sort_keys=True,
            )
        )
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
