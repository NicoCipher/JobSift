"""Read-only Lever public-postings contract audit.

The audit first freezes a cohort selected exclusively from the supplied
historical corpus.  Its live phase only calls Lever's public postings API and
stores request and identity evidence, never posting descriptions or forms.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).parent
INPUT = ROOT / "input" / "lever_historical_urls.csv"
COHORT_PATH = ROOT / "cohort.json"
DERIVATION_PATH = ROOT / "target_derivation.json"
RESULTS_PATH = ROOT / "live_results.json"
PROGRESS_PATH = ROOT / "live_progress.json"
CHECKPOINT_DIR = ROOT / "live_site_checkpoints"
FIELD_COVERAGE_PATH = ROOT / "field_coverage.json"
PAGINATION_PATH = ROOT / "pagination.json"
IDENTITY_PATH = ROOT / "identity_evidence.json"
FAILURE_PATH = ROOT / "failure_model_evidence.json"
SUMMARY_PATH = ROOT / "audit_summary.json"

PAGE_LIMIT = 50
GLOBAL_TARGETS = 8
MAX_RETRIES = 2
REQUEST_DELAY_SECONDS = 0.5
TIMEOUT_SECONDS = 20.0

API_HOSTS = {
    "global": "api.lever.co",
    "eu": "api.eu.lever.co",
}
JOB_HOSTS = {
    "global": "jobs.lever.co",
    "eu": "jobs.eu.lever.co",
}
FIELD_PATHS = {
    "id": ("id",),
    "text": ("text",),
    "hostedUrl": ("hostedUrl",),
    "applyUrl": ("applyUrl",),
    "description": ("description",),
    "descriptionPlain": ("descriptionPlain",),
    "descriptionBodyPlain": ("descriptionBodyPlain",),
    "categories.location": ("categories", "location"),
    "allLocations": ("categories", "allLocations"),
    "country": ("country",),
    "workplaceType": ("workplaceType",),
    "categories.commitment": ("categories", "commitment"),
    "team": ("categories", "team"),
    "department": ("categories", "department"),
    "createdAt": ("createdAt",),
    "salaryRange": ("salaryRange",),
}


def now() -> str:
    return datetime.now(UTC).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def checkpoint_path(instance: str, site: str) -> Path:
    return CHECKPOINT_DIR / f"{instance}__{site.casefold()}.json"


def checkpoint_hash(payload: dict[str, Any]) -> str:
    unhashed = dict(payload)
    unhashed.pop("checkpoint_sha256", None)
    return sha256(unhashed)


def write_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    payload = dict(payload)
    payload["checkpoint_sha256"] = checkpoint_hash(payload)
    write_json(path, payload)


def read_checkpoint(path: Path, cohort_sha256: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if payload.get("cohort_manifest_sha256") != cohort_sha256:
        return None
    if payload.get("checkpoint_sha256") != checkpoint_hash(payload):
        return None
    return payload


def read_rows() -> list[dict[str, str]]:
    with INPUT.open(encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


def safe_value(value: Any) -> bool:
    return value not in (None, "", [], {})


def nested_value(value: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def target_key(row: dict[str, str]) -> tuple[str, str, str]:
    return row["lever_instance"], row["host"], row["site_from_url"]


def valid_row(row: dict[str, str]) -> bool:
    return (
        row["lever_instance"] in API_HOSTS
        and row["host"] == JOB_HOSTS[row["lever_instance"]]
        and bool(row["site_from_url"].strip())
        and bool(row["posting_id_candidate"].strip())
    )


def representative(rows: list[dict[str, str]]) -> dict[str, str]:
    return min(rows, key=lambda row: (int(row["row"]), row["sheet"], row["url"]))


def build_cohort(rows: list[dict[str, str]]) -> dict[str, Any]:
    groups: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if valid_row(row):
            groups[target_key(row)].append(row)

    def rank(item: tuple[tuple[str, str, str], list[dict[str, str]]]) -> tuple[int, int, str]:
        key, grouped = item
        return (-len(grouped), -len({row["url"] for row in grouped}), key[2].casefold())

    eu = sorted((item for item in groups.items() if item[0][0] == "eu"), key=rank)
    global_sites = sorted((item for item in groups.items() if item[0][0] == "global"), key=rank)[
        :GLOBAL_TARGETS
    ]
    selected = [*global_sites, *eu]

    targets: list[dict[str, Any]] = []
    for key, grouped in selected:
        item = representative(grouped)
        targets.append(
            {
                "historical_company_string": item["company"],
                "historical_row_count": len(grouped),
                "host": key[1],
                "instance": key[0],
                "representative_historical_url": item["url"],
                "selection_rationale": (
                    "selected by deterministic historical ranking: all valid EU sites plus "
                    "the eight highest-ranked global sites, ordered by historical row count, "
                    "unique historical URLs, and casefolded site"
                ),
                "site": key[2],
                "unique_historical_urls": len({row["url"] for row in grouped}),
            }
        )
    payload = {
        "created_at": now(),
        "input_sha256": file_sha256(INPUT),
        "selection": {
            "eu": "all valid EU sites",
            "global_count": GLOBAL_TARGETS,
            "global": "highest historical rank by rows, unique URLs, then site",
            "historical_vacancies_not_inspected": True,
        },
        "targets": targets,
    }
    payload["manifest_sha256"] = sha256(payload)
    return payload


def expected_urls(instance: str, site: str, posting_id: str) -> tuple[str, str]:
    base = f"https://{JOB_HOSTS[instance]}/{site}/{posting_id}"
    return base, f"{base}/apply"


def derive_target_urls(rows: list[dict[str, str]], cohort: dict[str, Any]) -> dict[str, Any]:
    selected = {
        (target["instance"], target["host"], target["site"]) for target in cohort["targets"]
    }
    records: list[dict[str, Any]] = []
    for row in rows:
        if target_key(row) not in selected:
            continue
        parsed = urlsplit(row["url"])
        parts = [part for part in parsed.path.split("/") if part]
        posting_id = row["posting_id_candidate"]
        structurally_valid = (
            parsed.scheme == "https"
            and parsed.netloc == row["host"]
            and len(parts) >= 2
            and parts[0] == row["site_from_url"]
            and parts[1] == posting_id
        )
        hosted, apply = expected_urls(row["lever_instance"], row["site_from_url"], posting_id)
        records.append(
            {
                "apply_url_form": apply if posting_id else None,
                "expected_hosted_url": hosted if posting_id else None,
                "historical_url": row["url"],
                "host": row["host"],
                "instance": row["lever_instance"],
                "posting_id": posting_id or None,
                "site": row["site_from_url"],
                "structurally_valid": structurally_valid,
                "url_variant": (
                    "apply" if parts[2:3] == ["apply"] else "hosted" if posting_id else "site_only"
                ),
            }
        )
    return {
        "cohort_manifest_sha256": cohort["manifest_sha256"],
        "generated_at": now(),
        "redirects_encountered": [],
        "records": records,
        "summary": {
            "structurally_valid": sum(record["structurally_valid"] for record in records),
            "total_historical_rows_for_cohort": len(records),
            "url_variants": dict(Counter(record["url_variant"] for record in records)),
        },
    }


@dataclass
class RequestResult:
    attempts: int
    error: str | None
    payload: Any
    status_code: int | None


def request_json(client: httpx.Client, url: str, params: dict[str, Any]) -> RequestResult:
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            response = client.get(url, params=params)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            if attempt <= MAX_RETRIES:
                time.sleep(REQUEST_DELAY_SECONDS * attempt)
                continue
            return RequestResult(attempt, f"{type(exc).__name__}: {exc}", None, None)
        if response.status_code in {429, 500, 502, 503, 504} and attempt <= MAX_RETRIES:
            time.sleep(REQUEST_DELAY_SECONDS * attempt)
            continue
        if response.status_code != 200:
            return RequestResult(
                attempt, f"HTTP {response.status_code}", None, response.status_code
            )
        try:
            return RequestResult(attempt, None, response.json(), response.status_code)
        except ValueError as exc:
            return RequestResult(attempt, f"JSONDecodeError: {exc}", None, response.status_code)
    raise AssertionError("unreachable")


def page_signature(page: list[dict[str, Any]]) -> str:
    identifiers = [item.get("id") if isinstance(item, dict) else None for item in page]
    return sha256(identifiers)


def collect_board(
    client: httpx.Client, target: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    base = f"https://{API_HOSTS[target['instance']]}/v0/postings/{target['site']}"
    rows: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    signatures: set[str] = set()
    anomalies: list[str] = []
    failures: list[str] = []
    offset = 0
    while True:
        result = request_json(client, base, {"mode": "json", "skip": offset, "limit": PAGE_LIMIT})
        page_record: dict[str, Any] = {
            "attempts": result.attempts,
            "offset": offset,
            "status_code": result.status_code,
        }
        if result.error:
            page_record["error"] = result.error
            pages.append(page_record)
            failures.append(result.error)
            break
        if not isinstance(result.payload, list) or not all(
            isinstance(item, dict) for item in result.payload
        ):
            page_record["error"] = "expected JSON list of objects"
            pages.append(page_record)
            failures.append(page_record["error"])
            break
        signature = page_signature(result.payload)
        page_record.update({"rows": len(result.payload), "signature": signature})
        pages.append(page_record)
        if result.payload and signature in signatures:
            anomalies.append(f"repeated page signature at offset {offset}")
            break
        signatures.add(signature)
        rows.extend(result.payload)
        if not result.payload:
            break
        offset += len(result.payload)
        time.sleep(REQUEST_DELAY_SECONDS)

    identifiers = [
        item.get("id") for item in rows if isinstance(item.get("id"), str) and item["id"]
    ]
    duplicate_ids = sum(count - 1 for count in Counter(identifiers).values() if count > 1)
    empty_ids = len(rows) - len(identifiers)
    ordering = request_json(client, base, {"mode": "json", "skip": 0, "limit": PAGE_LIMIT})
    ordering_stable = None
    if (
        not ordering.error
        and isinstance(ordering.payload, list)
        and all(isinstance(item, dict) for item in ordering.payload)
    ):
        first_signature = pages[0].get("signature") if pages else None
        ordering_stable = page_signature(ordering.payload) == first_signature
    result = {
        "api_url": base,
        "board_status": "success"
        if not failures and not anomalies and not empty_ids
        else "partial",
        "current_posting_count": len(set(identifiers)),
        "duplicate_provider_ids": duplicate_ids,
        "empty_or_malformed_provider_ids": empty_ids,
        "historical_company_string": target["historical_company_string"],
        "host": target["host"],
        "instance": target["instance"],
        "ordering_repeat": {
            "attempts": ordering.attempts,
            "error": ordering.error,
            "stable_first_page": ordering_stable,
            "status_code": ordering.status_code,
        },
        "pages": pages,
        "pagination_anomalies": anomalies,
        "request_failures": failures,
        "rows_retrieved": len(rows),
        "site": target["site"],
        "unique_provider_ids": len(set(identifiers)),
    }
    return result, rows


def classify_site_outcome(board: dict[str, Any]) -> str:
    """Keep a transport failure distinct from a valid empty or partial board."""
    failures = board["request_failures"]
    if failures:
        failure = failures[-1]
        if "Timeout" in failure:
            return "request_timeout"
        if "Connect" in failure or "Network" in failure:
            return "connection_failure"
        if failure.startswith("HTTP "):
            return "http_failure"
        if failure == "expected JSON list of objects":
            return "malformed_unexpected_payload"
        return "other_operational_failure"
    if board["rows_retrieved"] == 0:
        return "empty_valid_board"
    return board["board_status"]


def field_coverage(all_rows: list[dict[str, Any]], cohort: dict[str, Any]) -> dict[str, Any]:
    total = len(all_rows)
    fields: dict[str, Any] = {}
    for name, path in FIELD_PATHS.items():
        count = sum(safe_value(nested_value(row, path)) for row in all_rows)
        classification = (
            "UNAVAILABLE" if not count else "RELIABLE" if count == total else "CONDITIONAL"
        )
        if name in {"createdAt", "salaryRange"}:
            classification = "CONDITIONAL" if count else "UNAVAILABLE"
        fields[name] = {"classification": classification, "present": count, "total": total}
    workplace_values = Counter(
        str(row["workplaceType"]) for row in all_rows if safe_value(row.get("workplaceType"))
    )
    countries = Counter(str(row["country"]) for row in all_rows if safe_value(row.get("country")))
    return {
        "cohort_manifest_sha256": cohort["manifest_sha256"],
        "fields": fields,
        "generated_at": now(),
        "observed_country_values": dict(sorted(countries.items())),
        "observed_workplace_type_values": dict(sorted(workplace_values.items())),
    }


def detail_evidence(
    client: httpx.Client, board_rows: list[tuple[dict[str, Any], list[dict[str, Any]]]]
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for board, rows in board_rows:
        item = next((row for row in rows if isinstance(row.get("id"), str) and row["id"]), None)
        if item is None:
            continue
        url = f"{board['api_url']}/{item['id']}"
        result = request_json(client, url, {"mode": "json"})
        detail = result.payload if isinstance(result.payload, dict) else None
        evidence.append(
            {
                "attempts": result.attempts,
                "detail_id_matches_list": detail is not None and detail.get("id") == item["id"],
                "detail_keys_added": sorted(set(detail or {}) - set(item)),
                "detail_keys_removed": sorted(set(item) - set(detail or {})),
                "error": result.error,
                "instance": board["instance"],
                "site": board["site"],
                "status_code": result.status_code,
            }
        )
        time.sleep(REQUEST_DELAY_SECONDS)
    return evidence


def failure_evidence(client: httpx.Client, cohort: dict[str, Any]) -> dict[str, Any]:
    target = cohort["targets"][0]
    base = f"https://{API_HOSTS[target['instance']]}/v0/postings"
    unknown_site = request_json(
        client, f"{base}/jobsift-contract-audit-unknown-site", {"mode": "json"}
    )
    unknown_detail = request_json(
        client,
        f"{base}/{target['site']}/00000000-0000-0000-0000-000000000000",
        {"mode": "json"},
    )
    return {
        "detail_404_probe": {
            "error": unknown_detail.error,
            "status_code": unknown_detail.status_code,
        },
        "generated_at": now(),
        "list_404_probe": {"error": unknown_site.error, "status_code": unknown_site.status_code},
        "not_safely_exercised": [
            "429",
            "5xx",
            "timeout/network",
            "malformed JSON",
            "blank provider ID",
        ],
        "recommended_mapping": {
            "404 board": "invalid_target",
            "404 detail after a list snapshot": "partial",
            "429": "rate_limited after bounded retry",
            "5xx": "provider_error after bounded retry",
            "blank provider id": "per-posting quarantine and partial",
            "malformed JSON": "parse_failure",
            "timeout or network": "network_failure after bounded retry",
        },
    }


def historical_vs_live(
    rows: list[dict[str, str]], cohort: dict[str, Any], boards: list[dict[str, Any]]
) -> dict[str, Any]:
    historical: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for row in rows:
        if row["posting_id_candidate"]:
            historical[target_key(row)].add(row["posting_id_candidate"])
    result: list[dict[str, Any]] = []
    for board in boards:
        key = board["instance"], board["host"], board["site"]
        live_ids = set(board["provider_ids"])
        historical_ids = historical[key]
        result.append(
            {
                "current_not_historical": len(live_ids - historical_ids),
                "historical_id_count": len(historical_ids),
                "historical_not_current": len(historical_ids - live_ids),
                "historical_still_visible": len(historical_ids & live_ids),
                "instance": key[0],
                "site": key[2],
            }
        )
    return {"cohort_manifest_sha256": cohort["manifest_sha256"], "sites": result}


def legacy_checkpoint_payload(
    board: dict[str, Any], detail: dict[str, Any] | None, cohort_sha256: str, observed_at: str
) -> dict[str, Any]:
    return {
        "attempts": [
            {
                "attempted_at": observed_at,
                "detail_observation": detail,
                "outcome": classify_site_outcome(board),
                "raw_artifact": {
                    "provider_ids_sha256": sha256(board["provider_ids"]),
                    "retained": False,
                },
                "site_result": board,
            }
        ],
        "cohort_manifest_sha256": cohort_sha256,
        "current_outcome": classify_site_outcome(board),
        "host": board["host"],
        "instance": board["instance"],
        "site": board["site"],
    }


def backfill_legacy_checkpoints(cohort: dict[str, Any]) -> int:
    """Preserve the completed aggregate run as immutable per-site evidence."""
    if not RESULTS_PATH.exists():
        return 0
    try:
        results = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
        identities = json.loads(IDENTITY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    manifest = cohort["manifest_sha256"]
    if results.get("cohort_manifest_sha256") != manifest:
        return 0
    details = {
        (item["instance"], item["site"]): item for item in identities.get("detail_samples", [])
    }
    count = 0
    for board in results.get("sites", []):
        path = checkpoint_path(board["instance"], board["site"])
        checkpoint = read_checkpoint(path, manifest)
        if checkpoint is not None:
            if checkpoint.get("current_outcome") == "success" and board["rows_retrieved"] == 0:
                checkpoint["current_outcome"] = "empty_valid_board"
                checkpoint["attempts"][-1]["outcome"] = "empty_valid_board"
                write_checkpoint(path, checkpoint)
            continue
        detail = details.get((board["instance"], board["site"]))
        write_checkpoint(
            path,
            legacy_checkpoint_payload(board, detail, manifest, results.get("generated_at", now())),
        )
        count += 1
    return count


def completed_checkpoints(cohort: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    completed: dict[tuple[str, str], dict[str, Any]] = {}
    for target in cohort["targets"]:
        checkpoint = read_checkpoint(
            checkpoint_path(target["instance"], target["site"]), cohort["manifest_sha256"]
        )
        if checkpoint and checkpoint.get("current_outcome") in {
            "success",
            "partial",
            "empty_valid_board",
        }:
            completed[(target["instance"], target["site"])] = checkpoint
    return completed


def live_audit(rows: list[dict[str, str]], cohort: dict[str, Any]) -> None:
    backfill_legacy_checkpoints(cohort)
    completed = completed_checkpoints(cohort)
    pending = [
        target
        for target in cohort["targets"]
        if (target["instance"], target["site"]) not in completed
    ]
    if not pending:
        return
    headers = {"User-Agent": "JobSift Lever contract audit (read-only)"}
    board_rows: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    details: list[dict[str, Any]] = []
    with httpx.Client(timeout=TIMEOUT_SECONDS, headers=headers, follow_redirects=False) as client:
        for target in pending:
            board, records = collect_board(client, target)
            board_rows.append((board, records))
            detail = detail_evidence(client, [(board, records)])
            details.extend(detail)
            snapshot = {
                "attempted_at": now(),
                "detail_observation": detail[0] if detail else None,
                "outcome": classify_site_outcome(board),
                "raw_artifact": {
                    "provider_ids_sha256": sha256(
                        sorted(
                            row["id"]
                            for row in records
                            if isinstance(row.get("id"), str) and row["id"]
                        )
                    ),
                    "retained": False,
                },
                "site_result": {
                    **board,
                    "provider_ids": sorted(
                        row["id"] for row in records if isinstance(row.get("id"), str) and row["id"]
                    ),
                },
            }
            existing = read_checkpoint(
                checkpoint_path(target["instance"], target["site"]), cohort["manifest_sha256"]
            )
            attempts = [*(existing or {}).get("attempts", []), snapshot]
            write_checkpoint(
                checkpoint_path(target["instance"], target["site"]),
                {
                    "attempts": attempts,
                    "cohort_manifest_sha256": cohort["manifest_sha256"],
                    "current_outcome": classify_site_outcome(board),
                    "host": target["host"],
                    "instance": target["instance"],
                    "site": target["site"],
                },
            )
            write_json(
                PROGRESS_PATH,
                {
                    "cohort_manifest_sha256": cohort["manifest_sha256"],
                    "completed_sites": sorted(
                        [site for _, site in completed] + [item[0]["site"] for item in board_rows]
                    ),
                    "generated_at": now(),
                },
            )
            time.sleep(REQUEST_DELAY_SECONDS)
        failures = failure_evidence(client, cohort)

    boards = []
    all_rows: list[dict[str, Any]] = []
    for board, records in board_rows:
        ids = sorted(row["id"] for row in records if isinstance(row.get("id"), str) and row["id"])
        boards.append({**board, "provider_ids": ids})
        all_rows.extend(records)
    results = {
        "cohort_manifest_sha256": cohort["manifest_sha256"],
        "generated_at": now(),
        "request_protocol": {
            "delay_seconds": REQUEST_DELAY_SECONDS,
            "page_limit": PAGE_LIMIT,
            "serialized": True,
            "timeout_seconds": TIMEOUT_SECONDS,
            "transient_retries": MAX_RETRIES,
        },
        "sites": boards,
    }
    coverage = field_coverage(all_rows, cohort)
    pagination = {
        "cohort_manifest_sha256": cohort["manifest_sha256"],
        "page_limit": PAGE_LIMIT,
        "sites": [
            {
                "ordering_repeat": board["ordering_repeat"],
                "pages": board["pages"],
                "pagination_anomalies": board["pagination_anomalies"],
                "site": board["site"],
            }
            for board in boards
        ],
    }
    identities = {
        "cohort_manifest_sha256": cohort["manifest_sha256"],
        "detail_samples": details,
        "historical_vs_live": historical_vs_live(rows, cohort, boards)["sites"],
        "site_duplicate_provider_ids": {
            board["site"]: board["duplicate_provider_ids"] for board in boards
        },
    }
    summary = {
        "boards_partial": sum(board["board_status"] == "partial" for board in boards),
        "boards_success": sum(board["board_status"] == "success" for board in boards),
        "boards_total": len(boards),
        "cohort_manifest_sha256": cohort["manifest_sha256"],
        "current_unique_provider_ids": sum(board["unique_provider_ids"] for board in boards),
        "detail_samples_successful": sum(
            item["status_code"] == 200 and item["detail_id_matches_list"] for item in details
        ),
        "generated_at": now(),
        "pagination_anomalies": sum(len(board["pagination_anomalies"]) for board in boards),
        "rows_retrieved": sum(board["rows_retrieved"] for board in boards),
    }
    write_json(RESULTS_PATH, results)
    write_json(FIELD_COVERAGE_PATH, coverage)
    write_json(PAGINATION_PATH, pagination)
    write_json(IDENTITY_PATH, identities)
    write_json(FAILURE_PATH, failures)
    write_json(SUMMARY_PATH, summary)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("freeze", "checkpoint", "live"), required=True)
    args = parser.parse_args()
    rows = read_rows()
    if args.phase == "freeze":
        cohort = build_cohort(rows)
        write_json(COHORT_PATH, cohort)
        write_json(DERIVATION_PATH, derive_target_urls(rows, cohort))
        return
    if not COHORT_PATH.exists():
        raise SystemExit("run --phase freeze before the live audit")
    cohort = json.loads(COHORT_PATH.read_text(encoding="utf-8"))
    expected = dict(cohort)
    manifest_sha256 = expected.pop("manifest_sha256", None)
    if manifest_sha256 != sha256(expected):
        raise SystemExit("cohort manifest hash mismatch")
    if cohort.get("input_sha256") != file_sha256(INPUT):
        raise SystemExit("historical input hash mismatch")
    if args.phase == "checkpoint":
        print(backfill_legacy_checkpoints(cohort))
        return
    live_audit(rows, cohort)


if __name__ == "__main__":
    main()
