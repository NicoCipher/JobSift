"""Resumable, read-only health validation for the historical target universe."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import statistics
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

ROOT = Path(__file__).parent
UNIVERSE = ROOT.parent / "target_universe_v1" / "historical_v1.json"
PILOT_MANIFEST = ROOT / "pilot_manifest.json"
PILOT_CHECKPOINTS = ROOT / "checkpoints"
PILOT_RESULTS = ROOT / "pilot_results.json"
PILOT_REPORT = ROOT / "report.md"
FULL_ROOT = ROOT / "full"
FULL_MANIFEST = FULL_ROOT / "manifest.json"
FULL_CHECKPOINTS = FULL_ROOT / "checkpoints"
FULL_RESULTS = FULL_ROOT / "results.json"
FULL_SUMMARY = FULL_ROOT / "summary.json"
FULL_REPORT = FULL_ROOT / "report.md"
FULL_LOCK = FULL_ROOT / "run.lock"
TARGET_UNIVERSE_COMMIT = "67147812f1adce64ce3498d8ded9ae6ce0136940"
SOURCES = ("greenhouse", "ashby", "workday", "lever")
CLASSIFICATIONS = (
    "active",
    "valid_empty",
    "invalid",
    "restricted",
    "rate_limited",
    "transient_failure",
    "malformed_response",
    "unprocessable",
)
CONTROLS = {
    "greenhouse": "greenhouse:gitlab",
    "ashby": "ashby:supabase",
    "workday": "workday:pennmutual.wd1.myworkdayjobs.com:pennmutual:_penn-careers",
    "lever": "lever:global:crosslaketech",
}
RETRIES = 2
TIMEOUT = 20.0
LEVER_PAGE_SIZE = 50
WORKDAY_POTENTIAL_CAP = 2000
SYSTEMIC_FAILURE_THRESHOLD = 5
# Compatibility names for the original pilot runner and its preserved tests.
CHECKPOINTS = PILOT_CHECKPOINTS


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


def _lock_is_active(path: Path) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        os.kill(value["pid"], 0)
    except (KeyError, OSError, ValueError, TypeError):
        return False
    return True


@contextlib.contextmanager
def full_run_lock() -> Any:
    """Prevent concurrent resumptions from issuing duplicate public requests."""
    FULL_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(FULL_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if _lock_is_active(FULL_LOCK):
            raise RuntimeError("a full target health batch is already running")
        FULL_LOCK.unlink(missing_ok=True)
        descriptor = os.open(FULL_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        payload = {"pid": os.getpid(), "started_at": datetime.now(UTC).isoformat()}
        os.write(descriptor, canonical(payload).encode())
        yield
    finally:
        os.close(descriptor)
        FULL_LOCK.unlink(missing_ok=True)


def select_pilot(records: list[dict[str, Any]], per_source: int = 25) -> list[dict[str, Any]]:
    """Select controls, high-occurrence targets, then evenly spaced ranks."""
    selected: list[dict[str, Any]] = []
    for source in SOURCES:
        ranked = sorted(
            (record for record in records if record["source"] == source),
            key=lambda record: (-record["historical_occurrence_count"], record["target_identity"]),
        )
        by_id = {record["target_identity"]: record for record in ranked}
        control = CONTROLS[source]
        if control not in by_id:
            raise ValueError(f"positive control missing from universe: {control}")
        candidates = [
            by_id[control],
            *ranked[:5],
            *(
                ranked[round(index * (len(ranked) - 1) / (per_source - 1))]
                for index in range(per_source)
            ),
            *ranked,
        ]
        choices: list[dict[str, Any]] = []
        chosen: set[str] = set()
        for record in candidates:
            if len(choices) >= per_source:
                break
            if record["target_identity"] not in chosen:
                choices.append(record)
                chosen.add(record["target_identity"])
        if len(choices) != per_source or control not in {r["target_identity"] for r in choices}:
            raise ValueError(f"unable to select {per_source} {source} targets with control")
        selected.extend(choices)
    return selected


def make_manifest(universe: dict[str, Any], *, generated_at: str) -> dict[str, Any]:
    """Build the original immutable pilot manifest."""
    records = select_pilot(universe["target_records"])
    manifest = {
        "validation": "target-universe-health-v1",
        "target_universe_commit": TARGET_UNIVERSE_COMMIT,
        "target_universe_sha256": artifact_sha(UNIVERSE),
        "generated_at": generated_at,
        "selection": "positive control + five highest occurrence + evenly spaced rank sample",
        "target_counts": dict(Counter(record["source"] for record in records)),
        "targets": records,
    }
    manifest["manifest_sha256"] = sha(manifest)
    return manifest


def load_manifest() -> dict[str, Any]:
    """Load and verify the original immutable 100-target pilot manifest."""
    universe = json.loads(UNIVERSE.read_text(encoding="utf-8"))
    expected_sha = artifact_sha(UNIVERSE)
    if PILOT_MANIFEST.exists():
        manifest = json.loads(PILOT_MANIFEST.read_text(encoding="utf-8"))
        unhashed = dict(manifest)
        actual = unhashed.pop("manifest_sha256", None)
        if actual != sha(unhashed) or manifest.get("target_universe_sha256") != expected_sha:
            raise ValueError("pilot manifest does not match the canonical target universe")
        if manifest.get("target_counts") != {source: 25 for source in SOURCES}:
            raise ValueError("pilot manifest does not contain exactly 25 targets per source")
        return manifest
    manifest = make_manifest(universe, generated_at=datetime.now(UTC).isoformat())
    write_json(PILOT_MANIFEST, manifest)
    return manifest


def ordered_full_targets(universe: dict[str, Any]) -> list[dict[str, Any]]:
    records = universe["target_records"]
    if len({record["target_identity"] for record in records}) != len(records):
        raise ValueError("canonical target universe has duplicate target identities")
    return sorted(
        records, key=lambda record: (SOURCES.index(record["source"]), record["target_identity"])
    )


def make_full_manifest(universe: dict[str, Any], *, generated_at: str) -> dict[str, Any]:
    targets = ordered_full_targets(universe)
    counts = dict(Counter(target["source"] for target in targets))
    expected_counts = {"greenhouse": 774, "ashby": 678, "workday": 560, "lever": 275}
    if counts != expected_counts or len(targets) != 2287:
        raise ValueError(f"unexpected canonical target counts: {counts}, total {len(targets)}")
    manifest = {
        "validation": "target-universe-health-v1-full",
        "health_contract_version": "v1",
        "target_universe_commit": TARGET_UNIVERSE_COMMIT,
        "target_universe_sha256": artifact_sha(UNIVERSE),
        "generated_at": generated_at,
        "source_counts": counts,
        "request_policy": {
            "concurrency": 1,
            "delay_seconds": 0.5,
            "timeout_seconds": TIMEOUT,
            "retries": RETRIES,
            "lever_page_size": LEVER_PAGE_SIZE,
            "workday_detail_requests": False,
            "workday_cap_recovery": False,
        },
        "targets": targets,
    }
    manifest["manifest_sha256"] = sha(manifest)
    return manifest


def _verify_manifest(manifest: dict[str, Any], *, full: bool) -> dict[str, Any]:
    unhashed = dict(manifest)
    actual = unhashed.pop("manifest_sha256", None)
    expected_counts = {"greenhouse": 774, "ashby": 678, "workday": 560, "lever": 275}
    if actual != sha(unhashed):
        raise ValueError("health manifest hash is invalid")
    if manifest.get("target_universe_sha256") != artifact_sha(UNIVERSE):
        raise ValueError("health manifest does not match the canonical target universe")
    if full and (
        manifest.get("validation") != "target-universe-health-v1-full"
        or manifest.get("source_counts") != expected_counts
        or len(manifest.get("targets", [])) != 2287
        or [target["target_identity"] for target in manifest["targets"]]
        != [
            target["target_identity"]
            for target in ordered_full_targets(json.loads(UNIVERSE.read_text()))
        ]
    ):
        raise ValueError("full manifest does not contain the exact canonical target universe")
    return manifest


def load_full_manifest() -> dict[str, Any]:
    if FULL_MANIFEST.exists():
        return _verify_manifest(json.loads(FULL_MANIFEST.read_text(encoding="utf-8")), full=True)
    universe = json.loads(UNIVERSE.read_text(encoding="utf-8"))
    manifest = make_full_manifest(universe, generated_at=datetime.now(UTC).isoformat())
    write_json(FULL_MANIFEST, manifest)
    return manifest


class HealthProbe:
    def __init__(
        self, client: httpx.Client, delay: float = 0.5, *, exact_lever_count: bool = False
    ) -> None:
        self.client, self.delay, self.exact_lever_count = client, delay, exact_lever_count

    def _request(
        self, method: str, url: str, **kwargs: Any
    ) -> tuple[httpx.Response | None, int, str | None]:
        for attempt in range(1, RETRIES + 2):
            try:
                response = self.client.request(method, url, **kwargs)
            except httpx.RequestError as exc:
                error = f"{type(exc).__name__}: {exc}"
                if attempt <= RETRIES:
                    self._pause(attempt)
                    continue
                return None, attempt, error
            if (response.status_code == 429 or response.status_code >= 500) and attempt <= RETRIES:
                self._pause(attempt)
                continue
            self._pause(1)
            return response, attempt, None
        raise AssertionError("unreachable")

    def _pause(self, attempt: int) -> None:
        if self.delay:
            time.sleep(self.delay * attempt)

    @staticmethod
    def _result(
        target: dict[str, Any], attempts: int, response: httpx.Response | None, error: str | None
    ) -> dict[str, Any]:
        return {
            "target_identity": target["target_identity"],
            "source": target["source"],
            "historical_occurrence_count": target["historical_occurrence_count"],
            "request_count": attempts,
            "http_status": response.status_code if response else None,
            "classification": "transient_failure",
            "error": error,
            "provider_reported_total": None,
            "current_postings": None,
            "inventory_exact": None,
            "inventory_note": None,
            "first_page_count": None,
        }

    @staticmethod
    def _http_classification(result: dict[str, Any], response: httpx.Response | None) -> bool:
        """Classify non-200 HTTP outcomes; return True when probing must stop."""
        if response is None:
            return True
        status = response.status_code
        if status == 404:
            result["classification"] = "invalid"
            result["error"] = "HTTP 404"
            return True
        if status in {401, 403}:
            result["classification"] = "restricted"
            result["error"] = f"HTTP {status}"
            return True
        if status == 422:
            result["classification"] = "unprocessable"
            result["error"] = "HTTP 422 provider rejected this target/request coordinate"
            return True
        if status == 429:
            result["classification"] = "rate_limited"
            result["error"] = "HTTP 429"
            return True
        if status >= 500:
            result["error"] = f"HTTP {status}"
            return True
        if status != 200:
            result["classification"] = "malformed_response"
            result["error"] = f"HTTP {status}"
            return True
        return False

    @staticmethod
    def _jobs_count(body: Any) -> int | None:
        jobs = body.get("jobs") if isinstance(body, dict) else None
        return len(jobs) if isinstance(jobs, list) else None

    def _probe_lever(
        self, target: dict[str, Any], url: str, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        response, attempts, error = self._request("GET", url, **kwargs)
        result = self._result(target, attempts, response, error)
        if self._http_classification(result, response):
            return result
        try:
            body = response.json()
        except ValueError:
            result["classification"] = "malformed_response"
            return result
        if not isinstance(body, list):
            result["classification"] = "malformed_response"
            return result
        first_page_count = len(body)
        result["first_page_count"] = first_page_count
        if not self.exact_lever_count:
            result["current_postings"] = first_page_count
            result["inventory_exact"] = first_page_count < LEVER_PAGE_SIZE
            result["classification"] = "active" if first_page_count else "valid_empty"
            result["error"] = None
            return result

        total, offset, seen = 0, 0, set()
        while True:
            signature = sha(body)
            if signature in seen:
                result["classification"] = "malformed_response"
                result["error"] = "repeated Lever page signature"
                result["current_postings"] = None
                result["inventory_exact"] = False
                return result
            seen.add(signature)
            total += len(body)
            if len(body) < LEVER_PAGE_SIZE:
                result["current_postings"] = total
                result["inventory_exact"] = True
                result["classification"] = "active" if total else "valid_empty"
                result["error"] = None
                return result
            offset += LEVER_PAGE_SIZE
            next_kwargs = {"params": {"mode": "json", "skip": offset, "limit": LEVER_PAGE_SIZE}}
            response, page_attempts, error = self._request("GET", url, **next_kwargs)
            result["request_count"] += page_attempts
            result["http_status"] = response.status_code if response else None
            if error:
                result["error"] = error
            if self._http_classification(result, response):
                result["current_postings"] = None
                result["inventory_exact"] = False
                return result
            try:
                body = response.json()
            except ValueError:
                body = None
            if not isinstance(body, list):
                result["classification"] = "malformed_response"
                result["error"] = "Lever response is not a JSON list"
                result["current_postings"] = None
                result["inventory_exact"] = False
                return result

    def probe(self, target: dict[str, Any]) -> dict[str, Any]:
        source, coordinates = target["source"], target["coordinates"]
        if source == "greenhouse":
            method, url, kwargs = (
                "GET",
                f"https://boards-api.greenhouse.io/v1/boards/{quote(coordinates['board'], safe='')}/jobs",
                {"params": {"content": "false"}},
            )
        elif source == "ashby":
            method, url, kwargs = (
                "GET",
                f"https://api.ashbyhq.com/posting-api/job-board/{quote(coordinates['board'], safe='')}",
                {"params": {"includeCompensation": "false"}},
            )
        elif source == "lever":
            host = "api.lever.co" if coordinates["instance"] == "global" else "api.eu.lever.co"
            url = f"https://{host}/v0/postings/{quote(coordinates['site'], safe='')}"
            return self._probe_lever(
                target, url, {"params": {"mode": "json", "skip": 0, "limit": LEVER_PAGE_SIZE}}
            )
        else:
            base = f"https://{coordinates['host']}/wday/cxs/{quote(coordinates['tenant'], safe='')}/{quote(coordinates['site'], safe='')}"
            method, url, kwargs = (
                "POST",
                f"{base}/jobs",
                {"json": {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}},
            )
        response, attempts, error = self._request(method, url, **kwargs)
        result = self._result(target, attempts, response, error)
        if self._http_classification(result, response):
            return result
        try:
            body = response.json()
            if source in {"greenhouse", "ashby"}:
                count = self._jobs_count(body)
                if count is None:
                    result["classification"] = "malformed_response"
                    return result
                result["first_page_count"] = count
                result["current_postings"] = count
                result["inventory_exact"] = True
            else:
                total = body.get("total") if isinstance(body, dict) else None
                if not isinstance(total, int) or total < 0:
                    result["classification"] = "malformed_response"
                    return result
                result["provider_reported_total"] = total
                result["current_postings"] = total
                result["inventory_exact"] = total < WORKDAY_POTENTIAL_CAP
                if total >= WORKDAY_POTENTIAL_CAP:
                    result["inventory_note"] = (
                        "provider-reported total may be capped at 2000; cap recovery was not run"
                    )
            result["classification"] = "active" if result["current_postings"] else "valid_empty"
            result["error"] = None
            return result
        except ValueError:
            result["classification"] = "malformed_response"
            return result


def checkpoint_path(target: dict[str, Any], *, full: bool = False) -> Path:
    root = FULL_CHECKPOINTS if full else CHECKPOINTS
    return root / f"{hashlib.sha256(target['target_identity'].encode()).hexdigest()}.json"


def completed(
    target: dict[str, Any], manifest: dict[str, Any], *, full: bool = False
) -> dict[str, Any] | None:
    path = checkpoint_path(target, full=full)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        if full:
            raise ValueError(f"corrupt full checkpoint: {path}") from exc
        return None
    if (
        value.get("manifest_sha256") != manifest["manifest_sha256"]
        or value.get("target_identity") != target["target_identity"]
        or value.get("source") != target["source"]
        or value.get("classification") not in CLASSIFICATIONS
    ):
        if full:
            raise ValueError(f"mismatched full checkpoint: {path}")
        return None
    return value


def classification_counts(values: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(value["classification"] for value in values)
    return {classification: counts[classification] for classification in CLASSIFICATIONS}


def inventory_value(value: dict[str, Any]) -> int | None:
    if value["classification"] not in {"active", "valid_empty"}:
        return None
    current = value.get("current_postings")
    return current if isinstance(current, int) and current >= 0 else None


def inventory_bucket(value: int) -> str:
    if value == 0:
        return "0"
    if value < 10:
        return "1-9"
    if value < 50:
        return "10-49"
    if value < 100:
        return "50-99"
    if value < 500:
        return "100-499"
    return "500+"


def median(values: list[int]) -> float | None:
    return statistics.median(values) if values else None


def summarize_full(values: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    if len(values) != len(manifest["targets"]):
        raise ValueError("cannot summarize incomplete full health validation")
    per_source: dict[str, Any] = {}
    for source in SOURCES:
        source_values = [value for value in values if value["source"] == source]
        inventories = [
            amount for value in source_values if (amount := inventory_value(value)) is not None
        ]
        active_inventories = [
            amount
            for value in source_values
            if value["classification"] == "active"
            and (amount := inventory_value(value)) is not None
        ]
        per_source[source] = {
            "targets": len(source_values),
            "classifications": classification_counts(source_values),
            "summed_current_posting_evidence": sum(inventories),
            "median_current_postings_per_active_target": median(active_inventories),
            "inventory_distribution": {
                bucket: sum(inventory_bucket(amount) == bucket for amount in inventories)
                for bucket in ("0", "1-9", "10-49", "50-99", "100-499", "500+")
            },
            "potentially_capped_workday_targets": sum(
                value.get("inventory_exact") is False
                for value in source_values
                if value["source"] == "workday"
            ),
        }
    inventories = [amount for value in values if (amount := inventory_value(value)) is not None]
    occurrence_buckets = (
        ("1", 1, 1),
        ("2-4", 2, 4),
        ("5-9", 5, 9),
        ("10-24", 10, 24),
        ("25+", 25, None),
    )
    historical_frequency = []
    for label, lower, upper in occurrence_buckets:
        bucket_values = [
            value
            for value in values
            if value["historical_occurrence_count"] >= lower
            and (upper is None or value["historical_occurrence_count"] <= upper)
        ]
        bucket_inventory = [
            amount for value in bucket_values if (amount := inventory_value(value)) is not None
        ]
        active_targets = sum(value["classification"] == "active" for value in bucket_values)
        historical_frequency.append(
            {
                "historical_occurrences": label,
                "targets": len(bucket_values),
                "active_targets": active_targets,
                "active_percentage": round(100 * active_targets / len(bucket_values), 2)
                if bucket_values
                else 0,
                "median_current_inventory": median(bucket_inventory),
            }
        )
    return {
        "manifest_sha256": manifest["manifest_sha256"],
        "completed_at": datetime.now(UTC).isoformat(),
        "target_count": len(values),
        "classification_reconciliation": classification_counts(values),
        "per_source": per_source,
        "overall": {
            "active_targets": sum(value["classification"] == "active" for value in values),
            "non_active_targets": sum(value["classification"] != "active" for value in values),
            "raw_current_posting_evidence": sum(inventories),
            "inventory_evidence_target_count": len(inventories),
            "targets_contributing_most_current_inventory": [
                {
                    "target_identity": value["target_identity"],
                    "source": value["source"],
                    "current_postings": inventory_value(value),
                    "inventory_exact": value["inventory_exact"],
                }
                for value in sorted(
                    (value for value in values if inventory_value(value) is not None),
                    key=lambda value: (-inventory_value(value), value["target_identity"]),
                )[:20]
            ],
        },
        "historical_frequency_vs_health": historical_frequency,
        "inventory_interpretation": "Raw source inventory only. It is neither a unique-job count nor Taiwo-match or delivery evidence.",
    }


def full_report(summary: dict[str, Any]) -> str:
    lines = [
        "# Full Historical Target Universe Health",
        "",
        f"Manifest: `{summary['manifest_sha256']}`",
        "",
        "| Source | Targets | Active | Empty | Invalid | Restricted | Rate limited | Transient | Malformed | Unprocessable | Raw inventory | Median active inventory |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for source in SOURCES:
        value = summary["per_source"][source]
        counts = value["classifications"]
        lines.append(
            f"| {source} | {value['targets']} | {counts['active']} | {counts['valid_empty']} | {counts['invalid']} | {counts['restricted']} | {counts['rate_limited']} | {counts['transient_failure']} | {counts['malformed_response']} | {counts['unprocessable']} | {value['summed_current_posting_evidence']} | {value['median_current_postings_per_active_target']} |"
        )
    lines.extend(
        [
            "",
            "## Historical frequency vs current health",
            "",
            "| Historical occurrences | Targets | Active % | Median inventory |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for value in summary["historical_frequency_vs_health"]:
        lines.append(
            f"| {value['historical_occurrences']} | {value['targets']} | {value['active_percentage']} | {value['median_current_inventory']} |"
        )
    lines.extend(["", summary["inventory_interpretation"], ""])
    return "\n".join(lines)


def write_full_progress(manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    values = [completed(target, manifest, full=True) for target in manifest["targets"]]
    completed_values = [value for value in values if value is not None]
    write_json(
        FULL_RESULTS,
        {
            "manifest_sha256": manifest["manifest_sha256"],
            "updated_at": datetime.now(UTC).isoformat(),
            "completed": len(completed_values),
            "total": len(manifest["targets"]),
            "results": completed_values,
        },
    )
    if len(completed_values) == len(manifest["targets"]):
        summary = summarize_full(completed_values, manifest)
        write_json(FULL_SUMMARY, summary)
        FULL_REPORT.write_text(full_report(summary), encoding="utf-8")
    return completed_values, len(manifest["targets"]) - len(completed_values)


def report_pilot(results: list[dict[str, Any]], manifest: dict[str, Any]) -> str:
    lines = [
        "# Target Universe Health Pilot",
        "",
        f"Manifest: `{manifest['manifest_sha256']}`",
        "",
        "| Source | Checked | Active | Empty | Invalid | Restricted | Rate limited | Transient | Malformed |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for source in SOURCES:
        values = [item for item in results if item["source"] == source]
        counts = Counter(item["classification"] for item in values)
        lines.append(
            f"| {source} | {len(values)} | {counts['active']} | {counts['valid_empty']} | {counts['invalid']} | {counts['restricted']} | {counts['rate_limited']} | {counts['transient_failure']} | {counts['malformed_response']} |"
        )
    lines.extend(
        [
            "",
            "Health is current-provider evidence only; it does not alter historical target evidence.",
            "",
        ]
    )
    return "\n".join(lines)


def report(results: list[dict[str, Any]], manifest: dict[str, Any]) -> str:
    """Backward-compatible public name for pilot reporting."""
    return report_pilot(results, manifest)


def run_pilot(delay: float) -> dict[str, Any]:
    manifest = load_manifest()
    with httpx.Client(
        timeout=httpx.Timeout(TIMEOUT),
        headers={"User-Agent": "JobSift/0.1 (+https://github.com/NicoCipher/JobSift)"},
    ) as client:
        probe = HealthProbe(client, delay)
        values = []
        for target in manifest["targets"]:
            value = completed(target, manifest) or probe.probe(target)
            if "manifest_sha256" not in value:
                value["manifest_sha256"] = manifest["manifest_sha256"]
                write_json(checkpoint_path(target), value)
            values.append(value)
    payload = {
        "manifest_sha256": manifest["manifest_sha256"],
        "completed_at": datetime.now(UTC).isoformat(),
        "results": values,
    }
    write_json(PILOT_RESULTS, payload)
    PILOT_REPORT.write_text(report_pilot(values, manifest), encoding="utf-8")
    return payload


def run_full(delay: float, batch_size: int) -> dict[str, Any]:
    with full_run_lock():
        manifest = load_full_manifest()
        pending = [
            target
            for target in manifest["targets"]
            if completed(target, manifest, full=True) is None
        ]
        batch = pending[:batch_size]
        source_failure_streak: dict[str, tuple[str, int]] = {}
        with httpx.Client(
            timeout=httpx.Timeout(TIMEOUT),
            headers={"User-Agent": "JobSift/0.1 (+https://github.com/NicoCipher/JobSift)"},
        ) as client:
            probe = HealthProbe(client, delay, exact_lever_count=True)
            for target in batch:
                value = probe.probe(target)
                value["manifest_sha256"] = manifest["manifest_sha256"]
                write_json(checkpoint_path(target, full=True), value)
                classification = value["classification"]
                if classification in {
                    "restricted",
                    "rate_limited",
                    "transient_failure",
                    "malformed_response",
                }:
                    previous, count = source_failure_streak.get(
                        target["source"], (classification, 0)
                    )
                    source_failure_streak[target["source"]] = (
                        classification,
                        count + 1 if previous == classification else 1,
                    )
                    if source_failure_streak[target["source"]][1] >= SYSTEMIC_FAILURE_THRESHOLD:
                        write_full_progress(manifest)
                        raise RuntimeError(
                            f"stopped {target['source']} after {SYSTEMIC_FAILURE_THRESHOLD} consecutive {classification} outcomes"
                        )
                else:
                    source_failure_streak.pop(target["source"], None)
        values, remaining = write_full_progress(manifest)
    return {"completed": len(values), "total": len(manifest["targets"]), "remaining": remaining}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=("pilot", "full"), default="pilot")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    if args.scope == "pilot":
        manifest = load_manifest()
        if args.status:
            done = sum(completed(target, manifest) is not None for target in manifest["targets"])
            print(
                json.dumps(
                    {
                        "completed": done,
                        "total": len(manifest["targets"]),
                        "manifest_sha256": manifest["manifest_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return
        print(json.dumps({"completed": len(run_pilot(args.delay)["results"])}, sort_keys=True))
        return
    manifest = load_full_manifest()
    if args.status:
        values, remaining = write_full_progress(manifest)
        print(
            json.dumps(
                {
                    "completed": len(values),
                    "total": len(manifest["targets"]),
                    "remaining": remaining,
                    "manifest_sha256": manifest["manifest_sha256"],
                },
                sort_keys=True,
            )
        )
        return
    print(json.dumps(run_full(args.delay, args.batch_size), sort_keys=True))


if __name__ == "__main__":
    main()
