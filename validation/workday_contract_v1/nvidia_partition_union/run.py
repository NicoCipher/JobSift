"""Resumable, read-only NVIDIA Workday CXS partition validator.

Run from a persistent terminal:
    python -m validation.workday_contract_v1.nvidia_partition_union.run --resume
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from job_scout.dedupe.resolver import DEDUPE_VERSION
from job_scout.matching.matcher import MATCHER_VERSION

EXPECTED_MANIFEST_SHA256 = "b3edc8bd82bac3f135654c532623158c3dc70872da15be1cbfa41d47e3d963f4"
EXPECTED_MATCHER_VERSION = "deterministic-v5"
EXPECTED_DEDUPE_VERSION = "dedupe-v1"
LIMIT = 20
USER_AGENT = "JobSift-workday-cap-union-validation/1.0 (read-only)"
ROOT = Path(__file__).resolve().parent


class Response(Protocol):
    status_code: int
    text: str

    def json(self) -> dict[str, Any]: ...


class Client(Protocol):
    def post(self, url: str, *, json: dict[str, Any]) -> Response: ...

    def get(self, url: str) -> Response: ...


class ManifestError(RuntimeError):
    pass


@dataclass(frozen=True)
class Partition:
    id: str
    label: str
    advertised_count: int


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def sha_json(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def page_signature(paths: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(paths).encode()).hexdigest()


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    with path.open("a") as handle:
        handle.write(json.dumps(value, sort_keys=True) + "\n")


def jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class Runner:
    def __init__(
        self,
        root: Path = ROOT,
        *,
        delay: float = 0.5,
        retries: int = 2,
        client: Client | None = None,
        expected_manifest_sha: str = EXPECTED_MANIFEST_SHA256,
    ) -> None:
        self.root = root
        self.run_dir = root / "run"
        self.delay = delay
        self.retries = retries
        self.client = client
        self.expected_manifest_sha = expected_manifest_sha
        self.manifest = self._load_manifest()
        self.partitions = tuple(Partition(**item) for item in self.manifest["partitions"])
        self.base = (
            f"https://{self.manifest['host']}/wday/cxs/"
            f"{self.manifest['tenant']}/{self.manifest['site']}"
        )

    @property
    def progress_path(self) -> Path:
        return self.run_dir / "progress.json"

    def _load_manifest(self) -> dict[str, Any]:
        path = self.root / "freeze.json"
        manifest = json.loads(path.read_text())
        claimed = manifest.pop("sha256", None)
        actual = sha_json(manifest)
        manifest["sha256"] = claimed
        if claimed != actual or claimed != self.expected_manifest_sha:
            raise ManifestError("freeze manifest hash differs from the approved contract")
        if (manifest.get("host"), manifest.get("tenant"), manifest.get("site")) != (
            "nvidia.wd5.myworkdayjobs.com",
            "nvidia",
            "NVIDIAExternalCareerSite",
        ):
            raise ManifestError("freeze manifest target differs from NVIDIA contract")
        partitions = manifest.get("partitions")
        if not isinstance(partitions, list) or len(partitions) != 15:
            raise ManifestError("freeze manifest partition set differs from approved 15 groups")
        ids = [item.get("id") for item in partitions]
        if any(not isinstance(item, str) or not item for item in ids) or len(set(ids)) != 15:
            raise ManifestError("freeze manifest has invalid or duplicate partition identifiers")
        if MATCHER_VERSION != EXPECTED_MATCHER_VERSION or DEDUPE_VERSION != EXPECTED_DEDUPE_VERSION:
            raise ManifestError("matcher or dedupe version differs from validation provenance")
        return manifest

    def _ensure_run(self) -> None:
        self.run_dir.mkdir(exist_ok=True)
        if self.progress_path.exists():
            progress = json.loads(self.progress_path.read_text())
            if progress.get("manifest_sha256") != self.manifest["sha256"]:
                raise ManifestError("existing run belongs to a different freeze manifest")
            return
        self._write_progress()

    def _write_progress(self) -> None:
        pages = jsonl(self.run_dir / "partition_pages.jsonl")
        paths = jsonl(self.run_dir / "external_paths.jsonl")
        details = jsonl(self.run_dir / "detail_identity.jsonl")
        errors = jsonl(self.run_dir / "errors.jsonl")
        complete = self._partition_completion(pages)
        payload = {
            "updated_at": utc_now(),
            "manifest_sha256": self.manifest["sha256"],
            "matcher_version": MATCHER_VERSION,
            "dedupe_version": DEDUPE_VERSION,
            "partitions_completed": sum(complete.values()),
            "partitions_total": len(self.partitions),
            "pages_completed": len(pages),
            "external_paths_discovered": len({item["external_path"] for item in paths}),
            "detail_identities_resolved": len(
                {item["external_path"] for item in details if item.get("job_req_id")}
            ),
            "errors_recorded": len(errors),
            "final_union_ready": self._union_is_ready(),
        }
        self.progress_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def _union_is_ready(self) -> bool:
        path = self.run_dir / "union_summary.json"
        if not path.exists():
            return False
        return "broad_query" in json.loads(path.read_text())

    def _partition_completion(self, pages: list[dict[str, Any]]) -> dict[str, bool]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for page in pages:
            if page.get("scope") == "partition" and page.get("status") == "success":
                grouped[page["partition_id"]].append(page)
        result = {}
        for partition in self.partitions:
            records = grouped[partition.id]
            first = next((item for item in records if item["offset"] == 0), None)
            if not first:
                result[partition.id] = False
                continue
            total = first["first_page_total"]
            offsets = {item["offset"] for item in records}
            result[partition.id] = all(offset in offsets for offset in range(0, total, LIMIT))
        return result

    def _request(
        self, method: str, url: str, *, payload: dict[str, Any] | None = None
    ) -> Response | None:
        assert self.client is not None
        for attempt in range(self.retries + 1):
            try:
                response = (
                    self.client.post(url, json=payload or {})
                    if method == "post"
                    else self.client.get(url)
                )
            except httpx.RequestError as exc:
                error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code < 500 and response.status_code != 429:
                    return response
                error = f"HTTP {response.status_code}"
            if attempt < self.retries:
                time.sleep(self.delay * (attempt + 1))
        append_jsonl(self.run_dir / "errors.jsonl", {"at": utc_now(), "url": url, "error": error})
        self._write_progress()
        return None

    def _run_partitions(self) -> None:
        pages_path = self.run_dir / "partition_pages.jsonl"
        paths_path = self.run_dir / "external_paths.jsonl"
        existing_pages = jsonl(pages_path)
        complete_offsets = {
            (item["partition_id"], item["offset"]): item
            for item in existing_pages
            if item.get("scope") == "partition" and item.get("status") == "success"
        }
        for partition in self.partitions:
            first = complete_offsets.get((partition.id, 0))
            if first is None:
                response = self._request(
                    "post",
                    f"{self.base}/jobs",
                    payload={
                        "appliedFacets": {"jobFamilyGroup": [partition.id]},
                        "limit": LIMIT,
                        "offset": 0,
                        "searchText": "",
                    },
                )
                if response is None:
                    continue
                if response.status_code != 200:
                    self._error("partition", partition.id, 0, f"HTTP {response.status_code}")
                    continue
                body = response.json()
                first_total = body.get("total")
                rows = body.get("jobPostings") or []
                if not isinstance(first_total, int):
                    self._error("partition", partition.id, 0, "first page has no integer total")
                    continue
                if not all(
                    isinstance(row.get("externalPath"), str) and row["externalPath"] for row in rows
                ):
                    self._error("partition", partition.id, 0, "page has missing externalPath")
                    continue
                first = self._checkpoint_page(
                    partition, 0, first_total, rows, pages_path, paths_path
                )
                complete_offsets[(partition.id, 0)] = first
            total = first["first_page_total"]
            prior_signatures = {
                item["signature"]
                for key, item in complete_offsets.items()
                if key[0] == partition.id
            }
            for offset in range(LIMIT, total, LIMIT):
                if (partition.id, offset) in complete_offsets:
                    continue
                response = self._request(
                    "post",
                    f"{self.base}/jobs",
                    payload={
                        "appliedFacets": {"jobFamilyGroup": [partition.id]},
                        "limit": LIMIT,
                        "offset": offset,
                        "searchText": "",
                    },
                )
                if response is None:
                    break
                if response.status_code != 200:
                    self._error("partition", partition.id, offset, f"HTTP {response.status_code}")
                    break
                rows = response.json().get("jobPostings") or []
                paths = [row.get("externalPath") for row in rows]
                if not all(isinstance(path, str) and path for path in paths):
                    self._error("partition", partition.id, offset, "page has missing externalPath")
                    break
                if page_signature([str(path) for path in paths]) in prior_signatures:
                    self._error("partition", partition.id, offset, "repeated page signature")
                    break
                record = self._checkpoint_page(
                    partition, offset, total, rows, pages_path, paths_path
                )
                prior_signatures.add(record["signature"])
                complete_offsets[(partition.id, offset)] = record

    def _checkpoint_page(
        self,
        partition: Partition,
        offset: int,
        first_total: int,
        rows: list[dict[str, Any]],
        pages_path: Path,
        paths_path: Path,
    ) -> dict[str, Any]:
        paths = [row.get("externalPath") for row in rows]
        if not all(isinstance(path, str) and path for path in paths):
            self._error("partition", partition.id, offset, "page has missing externalPath")
            return {
                "partition_id": partition.id,
                "offset": offset,
                "first_page_total": first_total,
                "signature": "",
            }
        paths = [str(path) for path in paths]
        record = {
            "at": utc_now(),
            "scope": "partition",
            "status": "success",
            "partition_id": partition.id,
            "partition_label": partition.label,
            "offset": offset,
            "first_page_total": first_total,
            "row_count": len(paths),
            "signature": page_signature(paths),
        }
        append_jsonl(pages_path, record)
        for path in paths:
            append_jsonl(
                paths_path,
                {
                    "partition_id": partition.id,
                    "partition_label": partition.label,
                    "external_path": path,
                },
            )
        self._write_progress()
        time.sleep(self.delay)
        return record

    def _resolve_details(self) -> None:
        paths = {
            item["external_path"]
            for name in ("external_paths.jsonl", "broad_external_paths.jsonl")
            for item in jsonl(self.run_dir / name)
        }
        resolved = {
            item["external_path"]
            for item in jsonl(self.run_dir / "detail_identity.jsonl")
            if item.get("job_req_id")
        }
        for path in sorted(paths - resolved):
            response = self._request("get", f"{self.base}{path}")
            if response is None:
                continue
            if response.status_code != 200:
                self._error("detail", path, None, f"HTTP {response.status_code}")
                continue
            job_req_id = (response.json().get("jobPostingInfo") or {}).get("jobReqId")
            if not isinstance(job_req_id, str) or not job_req_id:
                self._error("detail", path, None, "detail has no jobReqId")
                continue
            append_jsonl(
                self.run_dir / "detail_identity.jsonl",
                {
                    "at": utc_now(),
                    "external_path": path,
                    "job_req_id": job_req_id,
                },
            )
            self._write_progress()
            time.sleep(self.delay)

    def _run_broad_query(self) -> None:
        pages_path = self.run_dir / "broad_pages.jsonl"
        paths_path = self.run_dir / "broad_external_paths.jsonl"
        pages = {
            item["offset"]: item for item in jsonl(pages_path) if item.get("status") == "success"
        }
        first = pages.get(0)
        if first is None:
            response = self._request(
                "post",
                f"{self.base}/jobs",
                payload={"appliedFacets": {}, "limit": LIMIT, "offset": 0, "searchText": ""},
            )
            if response is None or response.status_code != 200:
                return
            body = response.json()
            total = body.get("total")
            rows = body.get("jobPostings") or []
            if not isinstance(total, int):
                self._error("broad", "broad", 0, "first page has no integer total")
                return
            first = self._checkpoint_broad_page(0, total, rows, pages_path, paths_path)
            pages[0] = first
        total = first["first_page_total"]
        signatures = {item["signature"] for item in pages.values()}
        for offset in range(LIMIT, total, LIMIT):
            if offset in pages:
                continue
            response = self._request(
                "post",
                f"{self.base}/jobs",
                payload={"appliedFacets": {}, "limit": LIMIT, "offset": offset, "searchText": ""},
            )
            if response is None or response.status_code != 200:
                return
            rows = response.json().get("jobPostings") or []
            paths = [row.get("externalPath") for row in rows]
            if not all(isinstance(path, str) and path for path in paths):
                self._error("broad", "broad", offset, "page has missing externalPath")
                return
            if page_signature([str(path) for path in paths]) in signatures:
                self._error("broad", "broad", offset, "repeated page signature")
                return
            record = self._checkpoint_broad_page(offset, total, rows, pages_path, paths_path)
            signatures.add(record["signature"])

    def _checkpoint_broad_page(
        self,
        offset: int,
        first_total: int,
        rows: list[dict[str, Any]],
        pages_path: Path,
        paths_path: Path,
    ) -> dict[str, Any]:
        paths = [str(row["externalPath"]) for row in rows]
        record = {
            "at": utc_now(),
            "scope": "broad",
            "status": "success",
            "offset": offset,
            "first_page_total": first_total,
            "row_count": len(paths),
            "signature": page_signature(paths),
        }
        append_jsonl(pages_path, record)
        for path in paths:
            append_jsonl(paths_path, {"external_path": path})
        self._write_progress()
        time.sleep(self.delay)
        return record

    def _error(self, scope: str, key: str, offset: int | None, error: str) -> None:
        append_jsonl(
            self.run_dir / "errors.jsonl",
            {
                "at": utc_now(),
                "scope": scope,
                "key": key,
                "offset": offset,
                "error": error,
            },
        )
        self._write_progress()

    def _finalize(self) -> None:
        pages = jsonl(self.run_dir / "partition_pages.jsonl")
        paths = jsonl(self.run_dir / "external_paths.jsonl")
        broad_pages = jsonl(self.run_dir / "broad_pages.jsonl")
        broad_paths = jsonl(self.run_dir / "broad_external_paths.jsonl")
        identities = jsonl(self.run_dir / "detail_identity.jsonl")
        errors = jsonl(self.run_dir / "errors.jsonl")
        if not all(self._partition_completion(pages).values()):
            return
        path_partitions: dict[str, set[str]] = defaultdict(set)
        for item in paths:
            path_partitions[item["external_path"]].add(item["partition_id"])
        path_ids = {
            item["external_path"]: item["job_req_id"]
            for item in identities
            if item.get("job_req_id")
        }
        if set(path_partitions) - set(path_ids):
            return
        broad_first = next((item for item in broad_pages if item["offset"] == 0), None)
        if broad_first is None:
            return
        broad_total = broad_first["first_page_total"]
        if {item["offset"] for item in broad_pages} != set(range(0, broad_total, LIMIT)):
            return
        broad_path_set = {item["external_path"] for item in broad_paths}
        if broad_path_set - set(path_ids):
            return
        id_partitions: dict[str, set[str]] = defaultdict(set)
        for path, partitions in path_partitions.items():
            id_partitions[path_ids[path]].update(partitions)
        rows = []
        for partition in self.partitions:
            partition_entries = [
                item["external_path"] for item in paths if item["partition_id"] == partition.id
            ]
            partition_paths = set(partition_entries)
            partition_ids = {path_ids[p] for p in partition_paths}
            first_page = next(
                item
                for item in pages
                if item.get("partition_id") == partition.id and item["offset"] == 0
            )
            partition_errors = [
                item
                for item in errors
                if item.get("scope") == "partition" and item.get("key") == partition.id
            ]
            rows.append(
                {
                    "partition_id": partition.id,
                    "partition_label": partition.label,
                    "advertised_facet_count": partition.advertised_count,
                    "first_page_total": first_page["first_page_total"],
                    "rows_retrieved": len(partition_entries),
                    "unique_external_paths": len(partition_paths),
                    "duplicate_external_paths": len(partition_entries) - len(partition_paths),
                    "unique_job_req_ids": len(partition_ids),
                    "duplicate_job_req_ids": len(partition_paths) - len(partition_ids),
                    "pagination_anomalies": len(partition_errors),
                    "internally_consistent": (
                        len(partition_entries) == first_page["first_page_total"]
                        and len(partition_entries) == len(partition_paths)
                        and len(partition_paths) == len(partition_ids)
                        and not partition_errors
                    ),
                }
            )
        with (self.run_dir / "partition_summary.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        summary = {
            "completed_at": utc_now(),
            "manifest_sha256": self.manifest["sha256"],
            "sum_partition_rows": len(paths),
            "unique_external_paths": len(path_partitions),
            "unique_job_req_ids": len(id_partitions),
            "duplicate_job_req_ids_across_partitions": sum(
                len(value) > 1 for value in id_partitions.values()
            ),
            "overlap_partition_counts": dict(
                Counter(len(value) for value in id_partitions.values())
            ),
            "broad_total": self.manifest["broad_total"],
            "broad_query": {
                "rows_retrieved": len(broad_paths),
                "unique_external_paths": len(broad_path_set),
                "unique_job_req_ids": len({path_ids[path] for path in broad_path_set}),
                "paths_absent_from_partition_union": len(broad_path_set - set(path_partitions)),
                "partition_paths_absent_from_broad": len(set(path_partitions) - broad_path_set),
                "job_req_ids_absent_from_partition_union": len(
                    {path_ids[path] for path in broad_path_set} - set(id_partitions)
                ),
            },
        }
        (self.run_dir / "union_summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n"
        )
        self._write_progress()

    def run(self) -> dict[str, Any]:
        self._ensure_run()
        if self.client is None:
            with httpx.Client(
                timeout=20,
                trust_env=False,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            ) as client:
                self.client = client
                self._run_partitions()
                self._resolve_details()
                self._run_broad_query()
                self._resolve_details()
                self._finalize()
        else:
            self._run_partitions()
            self._resolve_details()
            self._run_broad_query()
            self._resolve_details()
            self._finalize()
        self._write_progress()
        return self.status()

    def status(self) -> dict[str, Any]:
        self._ensure_run()
        self._write_progress()
        return json.loads(self.progress_path.read_text())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--resume", action="store_true")
    mode.add_argument("--status", action="store_true")
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--retries", type=int, default=2)
    args = parser.parse_args()
    if args.delay < 0 or args.retries < 0:
        parser.error("--delay and --retries must be non-negative")
    runner = Runner(delay=args.delay, retries=args.retries)
    print(json.dumps(runner.status() if args.status else runner.run(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
