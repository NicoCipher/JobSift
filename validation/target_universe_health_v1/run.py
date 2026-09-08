"""Resumable, list-only health pilot for Target Universe V1."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

ROOT = Path(__file__).parent
UNIVERSE = ROOT.parent / "target_universe_v1" / "historical_v1.json"
MANIFEST = ROOT / "pilot_manifest.json"
CHECKPOINTS = ROOT / "checkpoints"
RESULTS = ROOT / "pilot_results.json"
REPORT = ROOT / "report.md"
TARGET_UNIVERSE_COMMIT = "67147812f1adce64ce3498d8ded9ae6ce0136940"
SOURCES = ("greenhouse", "ashby", "workday", "lever")
CONTROLS = {
    "greenhouse": "greenhouse:gitlab",
    "ashby": "ashby:supabase",
    "workday": "workday:pennmutual.wd1.myworkdayjobs.com:pennmutual:_penn-careers",
    "lever": "lever:global:crosslaketech",
}
RETRIES = 2
TIMEOUT = 20.0


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


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
        source_records = choices
        if len(source_records) != per_source or control not in {
            r["target_identity"] for r in source_records
        }:
            raise ValueError(f"unable to select {per_source} {source} targets with control")
        selected.extend(source_records)
    return selected


def make_manifest(universe: dict[str, Any], *, generated_at: str) -> dict[str, Any]:
    records = select_pilot(universe["target_records"])
    manifest = {
        "validation": "target-universe-health-v1",
        "target_universe_commit": TARGET_UNIVERSE_COMMIT,
        "target_universe_sha256": hashlib.sha256(UNIVERSE.read_bytes()).hexdigest(),
        "generated_at": generated_at,
        "selection": "positive control + five highest occurrence + evenly spaced rank sample",
        "target_counts": dict(Counter(record["source"] for record in records)),
        "targets": records,
    }
    manifest["manifest_sha256"] = sha(manifest)
    return manifest


def load_manifest() -> dict[str, Any]:
    universe = json.loads(UNIVERSE.read_text(encoding="utf-8"))
    expected_sha = hashlib.sha256(UNIVERSE.read_bytes()).hexdigest()
    if MANIFEST.exists():
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        unhashed = dict(manifest)
        actual = unhashed.pop("manifest_sha256", None)
        if actual != sha(unhashed) or manifest.get("target_universe_sha256") != expected_sha:
            raise ValueError("pilot manifest does not match the canonical target universe")
        if manifest.get("target_counts") != {source: 25 for source in SOURCES}:
            raise ValueError("pilot manifest does not contain exactly 25 targets per source")
        return manifest
    manifest = make_manifest(universe, generated_at=datetime.now(UTC).isoformat())
    write_json(MANIFEST, manifest)
    return manifest


class HealthProbe:
    def __init__(self, client: httpx.Client, delay: float = 0.5) -> None:
        self.client, self.delay = client, delay

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
            return response, attempt, None
        raise AssertionError("unreachable")

    def _pause(self, attempt: int) -> None:
        if self.delay:
            time.sleep(self.delay * attempt)

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
            method, url, kwargs = (
                "GET",
                f"https://{host}/v0/postings/{quote(coordinates['site'], safe='')}",
                {"params": {"mode": "json", "skip": 0, "limit": 50}},
            )
        else:
            base = f"https://{coordinates['host']}/wday/cxs/{quote(coordinates['tenant'], safe='')}/{quote(coordinates['site'], safe='')}"
            method, url, kwargs = (
                "POST",
                f"{base}/jobs",
                {"json": {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}},
            )
        response, attempts, error = self._request(method, url, **kwargs)
        result: dict[str, Any] = {
            "target_identity": target["target_identity"],
            "source": source,
            "historical_occurrence_count": target["historical_occurrence_count"],
            "request_count": attempts,
            "http_status": response.status_code if response else None,
            "classification": "transient_failure",
            "error": error,
            "provider_reported_total": None,
            "first_page_count": None,
        }
        if response is None:
            return result
        if response.status_code == 404:
            result["classification"] = "invalid"
            return result
        if response.status_code in {401, 403}:
            result["classification"] = "restricted"
            return result
        if response.status_code == 429:
            result["classification"] = "rate_limited"
            return result
        if response.status_code >= 500:
            result["error"] = f"HTTP {response.status_code}"
            return result
        if response.status_code != 200:
            result["classification"] = "malformed_response"
            result["error"] = f"HTTP {response.status_code}"
            return result
        try:
            body = response.json()
            if source in {"greenhouse", "ashby"}:
                jobs = body.get("jobs") if isinstance(body, dict) else None
                count = len(jobs) if isinstance(jobs, list) else None
            elif source == "lever":
                count = len(body) if isinstance(body, list) else None
            else:
                total = body.get("total") if isinstance(body, dict) else None
                count = total if isinstance(total, int) and total >= 0 else None
                result["provider_reported_total"] = count
            if count is None:
                result["classification"] = "malformed_response"
                return result
            result["first_page_count"] = count if source != "workday" else None
            result["classification"] = "active" if count else "valid_empty"
            result["error"] = None
            return result
        except ValueError:
            result["classification"] = "malformed_response"
            return result


def checkpoint_path(target: dict[str, Any]) -> Path:
    return CHECKPOINTS / f"{hashlib.sha256(target['target_identity'].encode()).hexdigest()}.json"


def completed(target: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any] | None:
    path = checkpoint_path(target)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        value.get("manifest_sha256") != manifest["manifest_sha256"]
        or value.get("target_identity") != target["target_identity"]
    ):
        return None
    return value


def report(results: list[dict[str, Any]], manifest: dict[str, Any]) -> str:
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
    failures = [item for item in results if item["classification"] not in {"active", "valid_empty"}]
    if failures:
        lines.extend(["", "## Non-healthy outcomes", ""])
        for item in failures:
            detail = item["error"] or f"HTTP {item['http_status']}"
            lines.append(f"- `{item['target_identity']}` — {item['classification']} ({detail})")
    lines.extend(
        [
            "",
            "Health is current-provider evidence only; it does not alter historical target evidence.",
            "",
        ]
    )
    return "\n".join(lines)


def run(delay: float) -> dict[str, Any]:
    manifest = load_manifest()
    client = httpx.Client(
        timeout=httpx.Timeout(TIMEOUT),
        headers={"User-Agent": "JobSift/0.1 (+https://github.com/NicoCipher/JobSift)"},
    )
    try:
        probe = HealthProbe(client, delay)
        values = []
        for target in manifest["targets"]:
            value = completed(target, manifest) or probe.probe(target)
            if "manifest_sha256" not in value:
                value["manifest_sha256"] = manifest["manifest_sha256"]
                write_json(checkpoint_path(target), value)
            values.append(value)
    finally:
        client.close()
    payload = {
        "manifest_sha256": manifest["manifest_sha256"],
        "completed_at": datetime.now(UTC).isoformat(),
        "results": values,
    }
    write_json(RESULTS, payload)
    REPORT.write_text(report(values, manifest), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
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
    print(json.dumps({"completed": len(run(args.delay)["results"])}, sort_keys=True))


if __name__ == "__main__":
    main()
