"""Continuous canonical ATS target discovery with provider-authoritative admission."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from job_scout.production_registry import (
    ProductionSourceRegistry,
    ProductionTarget,
    load_production_registry,
    sha256_json,
)
from job_scout.storage.source_discovery import SourceDiscoveryStore
from job_scout.storage.sqlite import SQLiteRepository
from job_scout.target_universe import HistoricalLink, derive_link

COMMON_CRAWL_COLLECTIONS = "https://index.commoncrawl.org/collinfo.json"
DISCOVERY_SOURCE = "commoncrawl-cdx"
DISCOVERY_USER_AGENT = "JobSift-SourceDiscovery/1.0 (+https://github.com/NicoCipher/JobSift)"
HEALTH_USER_AGENT = "JobSift-SourceAdmission/1.0 (+https://github.com/NicoCipher/JobSift)"

@dataclass(frozen=True)
class DiscoveryQuery:
    query_id: str
    pattern: str


DISCOVERY_QUERIES = (
    DiscoveryQuery("greenhouse-global", "job-boards.greenhouse.io/*"),
    DiscoveryQuery("greenhouse-eu", "job-boards.eu.greenhouse.io/*"),
    DiscoveryQuery("greenhouse-legacy", "boards.greenhouse.io/*"),
    DiscoveryQuery("greenhouse-legacy-eu", "boards.eu.greenhouse.io/*"),
    DiscoveryQuery("ashby", "jobs.ashbyhq.com/*"),
    DiscoveryQuery("lever-global", "jobs.lever.co/*"),
    DiscoveryQuery("lever-eu", "jobs.eu.lever.co/*"),
    DiscoveryQuery("smartrecruiters", "jobs.smartrecruiters.com/*"),
    DiscoveryQuery("workday", "*.myworkdayjobs.com"),
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _company_hint(source: str, coordinates: dict[str, str]) -> str:
    if source in {"greenhouse", "ashby", "smartrecruiters"}:
        return coordinates["board"]
    if source == "lever":
        return coordinates["site"]
    return coordinates["tenant"]


def candidate_from_url(url: str) -> dict[str, Any] | None:
    """Extract only coordinates already supported by JobSift's canonical URL parser."""
    link = HistoricalLink(
        sheet="live-source-discovery",
        row=1,
        url=url,
        order=0,
    )
    derivation = derive_link(link)
    if derivation.classification != "supported_target":
        return None
    if not derivation.source or not derivation.coordinates or not derivation.identity:
        raise ValueError("supported discovery URL is missing canonical target coordinates")
    hint = _company_hint(derivation.source, derivation.coordinates)
    # Reuse the production model as the strict coordinate/identity validator.
    validated = ProductionTarget(
        target_identity=derivation.identity,
        source=derivation.source,
        coordinates=derivation.coordinates,
        company_hint=hint,
    )
    return {
        "target_identity": validated.target_identity,
        "source": validated.source,
        "coordinates": validated.coordinates,
        "company_hint": validated.company_hint,
    }


def overlay_admitted_targets(
    base: ProductionSourceRegistry,
    admitted: list[ProductionTarget],
) -> tuple[ProductionSourceRegistry, dict[str, Any]]:
    """Overlay health-current live targets without changing frozen baseline evidence."""
    by_identity = {target.target_identity: target for target in base.targets}
    dynamic: list[ProductionTarget] = []
    for target in admitted:
        existing = by_identity.get(target.target_identity)
        if existing is not None:
            if existing.source != target.source or existing.coordinates != target.coordinates:
                raise ValueError("live admission conflicts with frozen production target")
            continue
        by_identity[target.target_identity] = target
        dynamic.append(target)

    dynamic.sort(key=lambda target: target.target_identity)
    dynamic_digest = sha256_json(
        [target.model_dump(mode="json") for target in dynamic]
    )
    targets = sorted(by_identity.values(), key=lambda target: target.target_identity)
    counts = dict(sorted(Counter(target.source for target in targets).items()))
    registry_id = (
        base.registry_id
        if not dynamic
        else f"{base.registry_id}-live-{dynamic_digest[:16]}"
    )
    runtime = ProductionSourceRegistry(
        registry_id=registry_id,
        target_universe_git_blob_sha=base.target_universe_git_blob_sha,
        health_manifest_sha256=base.health_manifest_sha256,
        health_evidence_updated_at=base.health_evidence_updated_at,
        approval_policy=base.approval_policy,
        target_counts_by_source=counts,
        targets=targets,
    )
    return runtime, {
        "baseline_registry_id": base.registry_id,
        "baseline_targets": len(base.targets),
        "dynamic_admitted_targets": len(dynamic),
        "dynamic_admission_sha256": dynamic_digest,
        "runtime_registry_id": runtime.registry_id,
        "runtime_registry_sha256": sha256_json(runtime.model_dump(mode="json")),
        "runtime_targets": len(runtime.targets),
        "runtime_target_counts_by_source": runtime.target_counts_by_source,
    }


class CommonCrawlDiscovery:
    """Read bounded, distributed pages from the latest Common Crawl CDX index."""

    def __init__(
        self,
        client: httpx.Client,
        store: SourceDiscoveryStore,
        *,
        delay_seconds: float = 2.0,
        queries: tuple[DiscoveryQuery, ...] = DISCOVERY_QUERIES,
    ) -> None:
        self.client = client
        self.store = store
        self.delay_seconds = delay_seconds
        self.queries = queries

    def _pause(self) -> None:
        if self.delay_seconds:
            time.sleep(self.delay_seconds)

    def _get(self, url: str, *, params: Any = None) -> httpx.Response:
        response = self.client.get(url, params=params)
        self._pause()
        if response.status_code == 429 or response.status_code >= 500:
            raise RuntimeError(
                f"Common Crawl index unavailable: HTTP {response.status_code}"
            )
        response.raise_for_status()
        return response

    def latest_collection(self) -> tuple[str, str]:
        response = self._get(COMMON_CRAWL_COLLECTIONS)
        payload = response.json()
        if not isinstance(payload, list) or not payload:
            raise ValueError("Common Crawl collection list is empty or malformed")
        record = payload[0]
        crawl_id = record.get("id") if isinstance(record, dict) else None
        endpoint = record.get("cdx-api") if isinstance(record, dict) else None
        if not isinstance(crawl_id, str) or not isinstance(endpoint, str):
            raise ValueError("Common Crawl latest collection lacks CDX metadata")
        endpoint = endpoint.replace(
            "http://index.commoncrawl.org/",
            "https://index.commoncrawl.org/",
            1,
        )
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or parsed.hostname != "index.commoncrawl.org":
            raise ValueError("Common Crawl CDX endpoint is outside the approved origin")
        return crawl_id, endpoint

    def _page_count(self, endpoint: str, query: DiscoveryQuery) -> int:
        response = self._get(
            endpoint,
            params={
                "url": query.pattern,
                "output": "json",
                "pageSize": 1,
                "showNumPages": "true",
            },
        )
        payload = response.json()
        pages = payload.get("pages") if isinstance(payload, dict) else None
        if not isinstance(pages, int) or pages < 1:
            raise ValueError(f"Common Crawl page count is invalid for {query.query_id}")
        return pages

    @staticmethod
    def _initial_page(*, crawl_id: str, query_id: str, pages: int) -> int:
        if pages == 1:
            return 0
        digest = hashlib.sha256(f"{crawl_id}:{query_id}".encode()).hexdigest()
        return int(digest[:16], 16) % pages

    @staticmethod
    def _page_stride(*, query_id: str, pages: int) -> int:
        if pages == 1:
            return 1
        digest = hashlib.sha256(query_id.encode()).hexdigest()
        stride = int(digest[:16], 16) % pages or 1
        while math.gcd(stride, pages) != 1:
            stride = (stride + 1) % pages or 1
        return stride

    def _page(self, endpoint: str, query: DiscoveryQuery, page: int) -> list[dict[str, Any]]:
        params = [
            ("url", query.pattern),
            ("output", "json"),
            ("pageSize", "1"),
            ("page", str(page)),
            ("filter", "=status:200"),
            ("filter", "=mime:text/html"),
            ("collapse", "urlkey"),
            ("fields", "url,timestamp,status,mime"),
        ]
        response = self._get(endpoint, params=params)
        values: list[dict[str, Any]] = []
        for line in response.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except ValueError as exc:
                raise ValueError(
                    f"Common Crawl returned malformed JSON for {query.query_id}"
                ) from exc
            if not isinstance(value, dict) or not isinstance(value.get("url"), str):
                raise ValueError(
                    f"Common Crawl returned malformed record for {query.query_id}"
                )
            values.append(value)
        return values

    def discover(
        self,
        *,
        base_target_identities: set[str],
        observed_at: datetime,
    ) -> dict[str, Any]:
        crawl_id, endpoint = self.latest_collection()
        totals = Counter()
        source_counts = Counter()
        query_reports: list[dict[str, Any]] = []
        unique_run_targets: set[str] = set()

        for query in self.queries:
            try:
                cursor = self.store.get_cursor(
                    discovery_source=DISCOVERY_SOURCE,
                    query_id=query.query_id,
                )
                if cursor is None or cursor["crawl_id"] != crawl_id:
                    pages = self._page_count(endpoint, query)
                    page = self._initial_page(
                        crawl_id=crawl_id,
                        query_id=query.query_id,
                        pages=pages,
                    )
                else:
                    pages = int(cursor["pages"])
                    page = int(cursor["page"])
                records = self._page(endpoint, query, page)
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                query_reports.append(
                    {
                        "query_id": query.query_id,
                        "status": "failed",
                        "error": str(exc),
                    }
                )
                totals["queries_failed"] += 1
                continue

            query_unique: set[str] = set()
            query_supported = 0
            query_base = 0
            pending_observations: list[dict[str, Any]] = []
            for record in records:
                totals["index_records"] += 1
                candidate = candidate_from_url(record["url"])
                if candidate is None:
                    continue
                query_supported += 1
                identity = candidate["target_identity"]
                query_unique.add(identity)
                unique_run_targets.add(identity)
                source_counts[candidate["source"]] += 1
                if identity in base_target_identities:
                    query_base += 1
                    continue
                pending_observations.append(
                    {
                        **candidate,
                        "discovery_source": DISCOVERY_SOURCE,
                        "crawl_id": crawl_id,
                        "query_id": query.query_id,
                        "captured_at": str(record.get("timestamp") or ""),
                        "discovered_url": record["url"],
                        "observed_at": observed_at,
                    }
                )

            query_new, evidence_rows = self.store.observe_candidates(
                pending_observations
            )

            next_page = (
                page
                + self._page_stride(query_id=query.query_id, pages=pages)
            ) % pages
            self.store.set_cursor(
                discovery_source=DISCOVERY_SOURCE,
                query_id=query.query_id,
                crawl_id=crawl_id,
                page=next_page,
                pages=pages,
                updated_at=observed_at,
            )
            totals["queries_succeeded"] += 1
            totals["supported_urls"] += query_supported
            totals["already_in_frozen_registry"] += query_base
            totals["new_targets"] += query_new
            totals["evidence_rows_inserted"] += evidence_rows
            query_reports.append(
                {
                    "query_id": query.query_id,
                    "page": page,
                    "pages": pages,
                    "next_page": next_page,
                    "status": "success",
                    "index_records": len(records),
                    "supported_urls": query_supported,
                    "unique_targets": len(query_unique),
                    "already_in_frozen_registry": query_base,
                    "new_targets": query_new,
                    "evidence_rows_inserted": evidence_rows,
                }
            )

        return {
            "source": DISCOVERY_SOURCE,
            "crawl_id": crawl_id,
            **dict(totals),
            "unique_supported_targets_seen": len(unique_run_targets),
            "supported_urls_by_source": dict(sorted(source_counts.items())),
            "queries": query_reports,
        }


class SourceAdmissionProbe:
    """Cheap provider-origin health check. Discovery evidence alone never admits."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        delay_seconds: float = 0.1,
        retries: int = 2,
    ) -> None:
        self.client = client
        self.delay_seconds = delay_seconds
        self.retries = retries

    def _pause(self, multiplier: int = 1) -> None:
        if self.delay_seconds:
            time.sleep(self.delay_seconds * multiplier)

    def _request(self, method: str, url: str, **kwargs: Any):
        attempts = 0
        last_error: str | None = None
        response: httpx.Response | None = None
        for attempt in range(1, self.retries + 2):
            attempts = attempt
            try:
                response = self.client.request(method, url, **kwargs)
            except httpx.RequestError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt <= self.retries:
                    self._pause(attempt)
                    continue
                return None, attempts, last_error
            if (response.status_code == 429 or response.status_code >= 500) and attempt <= self.retries:
                self._pause(attempt)
                continue
            self._pause()
            return response, attempts, None
        return response, attempts, last_error

    @staticmethod
    def _base_result(
        response: httpx.Response | None,
        attempts: int,
        error: str | None,
    ) -> dict[str, Any]:
        return {
            "classification": "transient_failure",
            "request_count": attempts,
            "http_status": response.status_code if response is not None else None,
            "current_postings": None,
            "inventory_exact": None,
            "error": error,
        }

    @staticmethod
    def _http_stop(result: dict[str, Any], response: httpx.Response | None) -> bool:
        if response is None:
            return True
        status = response.status_code
        if status == 404:
            result.update(classification="invalid", error="HTTP 404")
            return True
        if status in {401, 403}:
            result.update(classification="restricted", error=f"HTTP {status}")
            return True
        if status == 422:
            result.update(classification="unprocessable", error="HTTP 422")
            return True
        if status == 429:
            result.update(classification="rate_limited", error="HTTP 429")
            return True
        if status >= 500:
            result.update(error=f"HTTP {status}")
            return True
        if status != 200:
            result.update(classification="malformed_response", error=f"HTTP {status}")
            return True
        return False

    def probe(self, target: dict[str, Any]) -> dict[str, Any]:
        source = target["source"]
        coordinates = target["coordinates"]
        if source == "greenhouse":
            method = "GET"
            url = (
                "https://boards-api.greenhouse.io/v1/boards/"
                f"{quote(coordinates['board'], safe='')}/jobs"
            )
            kwargs = {"params": {"content": "false"}}
        elif source == "ashby":
            method = "GET"
            url = (
                "https://api.ashbyhq.com/posting-api/job-board/"
                f"{quote(coordinates['board'], safe='')}"
            )
            kwargs = {"params": {"includeCompensation": "false"}}
        elif source == "smartrecruiters":
            method = "GET"
            url = (
                "https://api.smartrecruiters.com/v1/companies/"
                f"{quote(coordinates['board'], safe='')}/postings"
            )
            kwargs = {"params": {"destination": "PUBLIC", "limit": 1, "offset": 0}}
        elif source == "lever":
            method = "GET"
            host = (
                "api.lever.co"
                if coordinates["instance"] == "global"
                else "api.eu.lever.co"
            )
            url = f"https://{host}/v0/postings/{quote(coordinates['site'], safe='')}"
            kwargs = {"params": {"mode": "json", "skip": 0, "limit": 1}}
        elif source == "workday":
            method = "POST"
            base = (
                f"https://{coordinates['host']}/wday/cxs/"
                f"{quote(coordinates['tenant'], safe='')}/"
                f"{quote(coordinates['site'], safe='')}"
            )
            url = f"{base}/jobs"
            kwargs = {
                "json": {
                    "appliedFacets": {},
                    "limit": 1,
                    "offset": 0,
                    "searchText": "",
                }
            }
        else:
            raise ValueError(f"unsupported live discovery source: {source}")

        response, attempts, error = self._request(method, url, **kwargs)
        result = self._base_result(response, attempts, error)
        if self._http_stop(result, response):
            return result
        assert response is not None
        try:
            body = response.json()
            if source in {"greenhouse", "ashby"}:
                jobs = body.get("jobs") if isinstance(body, dict) else None
                if not isinstance(jobs, list):
                    raise ValueError("jobs list missing")
                current = len(jobs)
                exact = True
            elif source == "smartrecruiters":
                content = body.get("content") if isinstance(body, dict) else None
                current = body.get("totalFound") if isinstance(body, dict) else None
                offset = body.get("offset") if isinstance(body, dict) else None
                if (
                    not isinstance(content, list)
                    or not isinstance(current, int)
                    or current < 0
                    or offset != 0
                    or len(content) > 1
                    or bool(content) != bool(current)
                ):
                    raise ValueError("invalid SmartRecruiters health index")
                exact = True
            elif source == "lever":
                if not isinstance(body, list):
                    raise ValueError("Lever response is not a list")
                current = len(body)
                exact = current < 1
            else:
                current = body.get("total") if isinstance(body, dict) else None
                if not isinstance(current, int) or current < 0:
                    raise ValueError("Workday total missing")
                exact = current < 2000
        except (TypeError, ValueError):
            result.update(
                classification="malformed_response",
                error="provider health response is malformed",
            )
            return result

        result.update(
            classification="active" if current else "valid_empty",
            current_postings=current,
            inventory_exact=exact,
            error=None,
        )
        return result


def run_once(
    *,
    database: Path,
    base_registry_path: Path,
    output: Path,
    max_health_checks: int = 200,
    discovery_delay_seconds: float = 2.0,
    health_delay_seconds: float = 0.1,
    admission_max_age_hours: int = 48,
    skip_discovery: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = (now or utc_now()).astimezone(UTC)
    database.parent.mkdir(parents=True, exist_ok=True)
    repository = SQLiteRepository(database)
    store = SourceDiscoveryStore(repository)
    base = load_production_registry(base_registry_path)

    discovery: dict[str, Any]
    with httpx.Client(
        timeout=httpx.Timeout(30.0),
        headers={"User-Agent": DISCOVERY_USER_AGENT},
    ) as client:
        if skip_discovery:
            discovery = {"status": "skipped"}
        else:
            try:
                discovery = CommonCrawlDiscovery(
                    client,
                    store,
                    delay_seconds=discovery_delay_seconds,
                ).discover(
                    base_target_identities={
                        target.target_identity for target in base.targets
                    },
                    observed_at=current,
                )
                discovery["status"] = (
                    "partial" if discovery.get("queries_failed") else "success"
                )
            except (httpx.HTTPError, RuntimeError, TypeError, ValueError) as exc:
                # Common Crawl is discovery-only. Its outage must not discard the
                # already persisted admission queue or current health evidence.
                discovery = {"status": "failed", "error": str(exc)}

    candidates = store.health_candidates(now=current, limit=max_health_checks)
    classifications = Counter()
    newly_admitted = 0
    health_results: list[tuple[str, datetime, dict[str, Any]]] = []
    with httpx.Client(
        timeout=httpx.Timeout(20.0),
        headers={"User-Agent": HEALTH_USER_AGENT},
    ) as client:
        probe = SourceAdmissionProbe(
            client,
            delay_seconds=health_delay_seconds,
        )
        for candidate in candidates:
            before = candidate.get("latest_health_classification")
            result = probe.probe(candidate)
            health_results.append((candidate["target_identity"], current, result))
            classifications[result["classification"]] += 1
            if before != "active" and result["classification"] == "active":
                newly_admitted += 1
    store.record_health_batch(health_results)

    admitted, admission_health_evidence = store.admitted_snapshot(
        now=current,
        max_health_age_hours=admission_max_age_hours,
    )
    runtime, overlay = overlay_admitted_targets(base, admitted)
    overlay["health_evidence"] = admission_health_evidence
    overlay["health_evidence_sha256"] = sha256_json(
        admission_health_evidence
    )
    payload = {
        "discovery_version": "live-source-discovery-v1",
        "generated_at": current.isoformat(),
        "discovery": discovery,
        "health": {
            "checked": len(candidates),
            "classifications": dict(sorted(classifications.items())),
            "newly_admitted": newly_admitted,
        },
        "store": store.summary(
            now=current,
            max_health_age_hours=admission_max_age_hours,
        ),
        "runtime_overlay": overlay,
        "runtime_registry_sha256": sha256_json(runtime.model_dump(mode="json")),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m job_scout.source_discovery")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run")
    run.add_argument("--database", type=Path, required=True)
    run.add_argument("--base-registry", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--max-health-checks", type=int, default=200)
    run.add_argument("--discovery-delay-seconds", type=float, default=2.0)
    run.add_argument("--health-delay-seconds", type=float, default=0.1)
    run.add_argument("--admission-max-age-hours", type=int, default=48)
    run.add_argument("--skip-discovery", action="store_true")

    status = commands.add_parser("status")
    status.add_argument("--database", type=Path, required=True)
    status.add_argument("--base-registry", type=Path, required=True)
    status.add_argument("--admission-max-age-hours", type=int, default=48)

    args = parser.parse_args()
    if args.command == "run":
        if args.max_health_checks < 1:
            parser.error("--max-health-checks must be positive")
        payload = run_once(
            database=args.database,
            base_registry_path=args.base_registry,
            output=args.output,
            max_health_checks=args.max_health_checks,
            discovery_delay_seconds=args.discovery_delay_seconds,
            health_delay_seconds=args.health_delay_seconds,
            admission_max_age_hours=args.admission_max_age_hours,
            skip_discovery=args.skip_discovery,
        )
        print(json.dumps(payload, sort_keys=True))
        return

    repository = SQLiteRepository(args.database)
    store = SourceDiscoveryStore(repository)
    base = load_production_registry(args.base_registry)
    current = utc_now()
    admitted, admission_health_evidence = store.admitted_snapshot(
        now=current,
        max_health_age_hours=args.admission_max_age_hours,
    )
    runtime, overlay = overlay_admitted_targets(base, admitted)
    overlay["health_evidence"] = admission_health_evidence
    overlay["health_evidence_sha256"] = sha256_json(
        admission_health_evidence
    )
    payload = {
        "store": store.summary(
            now=current,
            max_health_age_hours=args.admission_max_age_hours,
        ),
        "runtime_overlay": overlay,
        "runtime_registry_sha256": sha256_json(runtime.model_dump(mode="json")),
    }
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
