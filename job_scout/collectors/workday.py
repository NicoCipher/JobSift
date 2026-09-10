"""Public Workday CXS collector.

This adapter uses only the board's public CXS search and posting-detail
endpoints.  It deliberately does not infer tenant/site values from a URL, scrape
rendered pages, or treat a capped broad result as a complete board.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from pydantic import ValidationError

from job_scout.domain.models import (
    CollectionResult,
    CollectionStatus,
    EmploymentType,
    Job,
    RemoteStatus,
    SourceTarget,
    WorkdayTargetConfig,
)
from job_scout.normalization.core import canonicalize_url, html_to_text
from job_scout.normalization.location import normalize_location

LIMIT = 20
CAP_TOTAL = 2000
RETRIES = 2
USER_AGENT = "JobSift/0.1 (+https://github.com/NicoCipher/JobSift)"
_REMOTE_TYPES = {
    "remote": RemoteStatus.REMOTE,
    "hybrid": RemoteStatus.HYBRID,
    "in office": RemoteStatus.ONSITE,
}
_EMPLOYMENT_TYPES = {
    "full time": EmploymentType.FULL_TIME,
    "part time": EmploymentType.PART_TIME,
}
_COUNTRY_ALIASES = {
    "united states of america": "United States",
    "united states": "United States",
    "usa": "United States",
    "us": "United States",
    "united kingdom": "United Kingdom",
    "uk": "United Kingdom",
    "canada": "Canada",
    "nigeria": "Nigeria",
    "india": "India",
}
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*")


@dataclass(frozen=True)
class _Partition:
    identifier: str
    label: str
    advertised_count: int


@dataclass(frozen=True)
class _RequestFailure:
    """A request/search failure with a machine-readable category."""

    category: str
    message: str
    status_code: int | None = None


@dataclass
class _QueryResult:
    paths: list[str]
    total: int | None
    errors: list[str]
    facets: list[dict[str, Any]]
    partition_paths: dict[str, set[str]]
    initial_failure: _RequestFailure | None

    @property
    def complete(self) -> bool:
        return self.total is not None and not self.errors and len(self.paths) == self.total


class WorkdayCollector:
    """Collect one explicit Workday host/tenant/site target through public CXS."""

    source = "workday"

    def __init__(self, client: httpx.Client | None = None, *, delay: float = 0.5) -> None:
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(20.0),
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            transport=httpx.HTTPTransport(retries=2),
        )
        self.delay = delay
        self.last_counts: dict[str, int | str | bool] = {}

    def collect(self, target: SourceTarget) -> CollectionResult:
        self.last_counts = {
            "broad_total": 0,
            "paths_discovered": 0,
            "detail_attempts": 0,
            "normalized": 0,
            "quarantined": 0,
            "partition_count": 0,
            "coverage_mode": "unknown",
        }
        config, configuration_error = self._configuration(target)
        if configuration_error:
            return self._failure(target, CollectionStatus.INVALID_TARGET, configuration_error)
        assert config is not None
        try:
            broad = self._search_all(config, {}, scope="broad")
        except httpx.RequestError as exc:
            return self._failure(target, CollectionStatus.NETWORK_FAILURE, type(exc).__name__)
        except (TypeError, ValueError, ValidationError) as exc:
            return self._failure(target, CollectionStatus.PARSE_FAILURE, type(exc).__name__)
        if broad.total is None:
            status = (
                self._status_for_failure(broad.initial_failure)
                if broad.initial_failure
                else CollectionStatus.PARSE_FAILURE
            )
            return self._failure(
                target, status, broad.errors[0] if broad.errors else "missing broad total"
            )

        self.last_counts["broad_total"] = broad.total
        errors = list(broad.errors)
        paths = list(broad.paths)
        partition_paths: dict[str, set[str]] = {}
        recovered = False
        if broad.total == CAP_TOTAL:
            partitions, partition_error = self._job_family_partitions(broad.facets)
            if partition_error:
                errors.append(partition_error)
                self.last_counts["coverage_mode"] = "capped_partial"
            else:
                assert partitions is not None
                self.last_counts["partition_count"] = len(partitions)
                partition_results = []
                for partition in partitions:
                    result = self._search_all(
                        config,
                        {"jobFamilyGroup": [partition.identifier]},
                        scope=f"jobFamilyGroup:{partition.identifier}",
                    )
                    partition_results.append((partition, result))
                    paths.extend(result.paths)
                    partition_paths[partition.identifier] = set(result.paths)
                    errors.extend(result.errors)
                    if result.total != partition.advertised_count:
                        errors.append(
                            "jobFamilyGroup partition "
                            f"{partition.identifier} total {result.total!r} differs from advertised "
                            f"{partition.advertised_count}"
                        )
                recovered = not errors and all(result.complete for _, result in partition_results)
                self.last_counts["coverage_mode"] = (
                    "job_family_group_partition" if recovered else "capped_partial"
                )
        elif broad.total < CAP_TOTAL:
            self.last_counts["coverage_mode"] = "broad"
        else:
            errors.append(
                f"broad total {broad.total} exceeds the verified {CAP_TOTAL} cap contract"
            )
            self.last_counts["coverage_mode"] = "capped_partial"

        unique_paths = list(dict.fromkeys(paths))
        self.last_counts["paths_discovered"] = len(unique_paths)
        jobs, detail_errors, path_ids = self._resolve_details(config, target, unique_paths)
        errors.extend(detail_errors)
        if recovered and self._partition_identity_conflict(partition_paths, path_ids):
            errors.append("jobFamilyGroup partitions overlap by provider jobReqId")
            self.last_counts["coverage_mode"] = "capped_partial"
        self.last_counts.update(normalized=len(jobs), quarantined=len(errors))
        return CollectionResult(
            source=self.source,
            target=target,
            status=CollectionStatus.PARTIAL if errors else CollectionStatus.SUCCESS,
            jobs=jobs,
            errors=errors,
        )

    def _configuration(self, target: SourceTarget) -> tuple[WorkdayTargetConfig | None, str | None]:
        config = target.workday
        if config is None:
            return None, "Workday requires explicit host, tenant, and site configuration"
        if not _HOST.fullmatch(config.host):
            return None, "Workday host is not a valid hostname"
        if target.board_id != config.board_id:
            return None, "board_id must equal explicit Workday host:tenant:site configuration"
        return config, None

    @staticmethod
    def _base(config: WorkdayTargetConfig) -> str:
        return f"https://{config.host}/wday/cxs/{quote(config.tenant, safe='')}/{quote(config.site, safe='')}"

    def _search_all(
        self,
        config: WorkdayTargetConfig,
        facets: dict[str, list[str]],
        *,
        scope: str,
    ) -> _QueryResult:
        paths: list[str] = []
        signatures: set[str] = set()
        first_total: int | None = None
        result_facets: list[dict[str, Any]] = []
        errors: list[str] = []
        initial_failure: _RequestFailure | None = None
        offset = 0
        while first_total is None or offset < first_total:
            body, failure = self._search_page(config, facets, offset)
            if failure:
                errors.append(f"{scope} offset {offset}: {failure.message}")
                initial_failure = failure
                break
            assert body is not None
            if first_total is None:
                total = body.get("total")
                if not isinstance(total, int) or total < 0:
                    errors.append(f"{scope} offset 0: response has no non-negative integer total")
                    break
                first_total = total
                raw_facets = body.get("facets") or []
                result_facets = raw_facets if isinstance(raw_facets, list) else []
            rows = body.get("jobPostings") or []
            if not isinstance(rows, list):
                errors.append(f"{scope} offset {offset}: jobPostings is not a list")
                break
            page_paths = [
                item.get("externalPath") if isinstance(item, dict) else None for item in rows
            ]
            if not all(self._valid_external_path(value) for value in page_paths):
                errors.append(f"{scope} offset {offset}: page has an invalid externalPath")
                break
            signature = hashlib.sha256("\n".join(page_paths).encode()).hexdigest()
            if signature in signatures:
                errors.append(f"{scope} offset {offset}: repeated page signature")
                break
            signatures.add(signature)
            paths.extend(page_paths)
            if len(rows) == 0 and offset < first_total:
                errors.append(f"{scope} offset {offset}: empty page before declared total")
                break
            offset += LIMIT
            self._pause()
        if first_total is not None and len(paths) != first_total and not errors:
            errors.append(
                f"{scope}: retrieved {len(paths)} rows but first page declared {first_total}"
            )
        if len(set(paths)) != len(paths) and not errors:
            errors.append(f"{scope}: duplicate externalPath values across pages")
        return _QueryResult(paths, first_total, errors, result_facets, {}, initial_failure)

    def _search_page(
        self, config: WorkdayTargetConfig, facets: dict[str, list[str]], offset: int
    ) -> tuple[dict[str, Any] | None, _RequestFailure | None]:
        response, failure = self._request(
            "post",
            f"{self._base(config)}/jobs",
            payload={"appliedFacets": facets, "limit": LIMIT, "offset": offset, "searchText": ""},
        )
        if response is None:
            return None, failure
        if response.status_code != 200:
            return None, _RequestFailure(
                "http_status", f"HTTP {response.status_code}", response.status_code
            )
        try:
            body = response.json()
        except ValueError:
            return None, _RequestFailure("parse", "response is not JSON")
        return (
            (body, None)
            if isinstance(body, dict)
            else (None, _RequestFailure("parse", "response is not an object"))
        )

    def _request(
        self, method: str, url: str, *, payload: dict[str, Any] | None = None
    ) -> tuple[httpx.Response | None, _RequestFailure | None]:
        """Make one serial CXS request with bounded transient retries.

        HTTPTransport's retries cover only a subset of connection failures. CXS
        detail reads can also raise ReadTimeout directly from Client.get, so this
        wrapper makes request-level failures explicit collector evidence.
        """
        failure = _RequestFailure("parse", "request did not run")
        for attempt in range(RETRIES + 1):
            try:
                response = (
                    self.client.post(url, json=payload or {})
                    if method == "post"
                    else self.client.get(url)
                )
            except httpx.RequestError as exc:
                failure = _RequestFailure("network", f"network {type(exc).__name__}: {exc}")
            else:
                if response.status_code != 429 and response.status_code < 500:
                    return response, None
                failure = _RequestFailure(
                    "http_status", f"HTTP {response.status_code}", response.status_code
                )
            if attempt < RETRIES:
                self._pause()
        return None, failure

    @staticmethod
    def _valid_external_path(value: object) -> bool:
        if not isinstance(value, str) or not value.startswith("/job/"):
            return False
        parsed = urlsplit(value)
        return not parsed.scheme and not parsed.netloc and not parsed.query and not parsed.fragment

    @staticmethod
    def _job_family_partitions(
        facets: list[dict[str, Any]],
    ) -> tuple[list[_Partition] | None, str | None]:
        facet = next(
            (
                item
                for item in facets
                if isinstance(item, dict) and item.get("facetParameter") == "jobFamilyGroup"
            ),
            None,
        )
        values = facet.get("values") if isinstance(facet, dict) else None
        if not isinstance(values, list) or len(values) < 2:
            return None, "broad result is capped and has no safe jobFamilyGroup partition contract"
        partitions = []
        for value in values:
            if not isinstance(value, dict):
                return None, "jobFamilyGroup partition metadata is malformed"
            identifier, label, count = value.get("id"), value.get("descriptor"), value.get("count")
            if (
                not isinstance(identifier, str)
                or not identifier
                or not isinstance(label, str)
                or not label
                or not isinstance(count, int)
                or count < 0
                or count >= CAP_TOTAL
            ):
                return None, "jobFamilyGroup partition metadata is unsafe for cap recovery"
            partitions.append(_Partition(identifier, label, count))
        if len({item.identifier for item in partitions}) != len(partitions):
            return None, "jobFamilyGroup partition identifiers are not unique"
        if sum(item.advertised_count for item in partitions) <= CAP_TOTAL:
            return None, "jobFamilyGroup partition counts do not establish coverage beyond the cap"
        return partitions, None

    def _resolve_details(
        self,
        config: WorkdayTargetConfig,
        target: SourceTarget,
        paths: list[str],
    ) -> tuple[list[Job], list[str], dict[str, str]]:
        jobs: dict[str, Job] = {}
        path_ids: dict[str, str] = {}
        errors: list[str] = []
        for path in paths:
            self.last_counts["detail_attempts"] = int(self.last_counts["detail_attempts"]) + 1
            response, failure = self._request("get", f"{self._base(config)}{quote(path, safe='/')}")
            if response is None:
                assert failure is not None
                errors.append(f"detail {path}: {failure.message}")
                self._pause()
                continue
            if response.status_code != 200:
                errors.append(f"detail {path}: HTTP {response.status_code}")
                self._pause()
                continue
            try:
                body = response.json()
                if not isinstance(body, dict):
                    raise TypeError("response is not an object")
                job = self._normalize(body, target, config, path)
            except (TypeError, ValueError, ValidationError) as exc:
                errors.append(f"detail {path}: {type(exc).__name__}")
                self._pause()
                continue
            path_ids[path] = job.source_job_id
            jobs.setdefault(job.source_job_id, job)
            self._pause()
        return list(jobs.values()), errors, path_ids

    @staticmethod
    def _partition_identity_conflict(
        partition_paths: dict[str, set[str]], path_ids: dict[str, str]
    ) -> bool:
        identity_partitions: dict[str, set[str]] = defaultdict(set)
        for partition, paths in partition_paths.items():
            for path in paths:
                if path in path_ids:
                    identity_partitions[path_ids[path]].add(partition)
        return any(len(partitions) > 1 for partitions in identity_partitions.values())

    def _normalize(
        self,
        body: dict[str, Any],
        target: SourceTarget,
        config: WorkdayTargetConfig,
        expected_path: str,
    ) -> Job:
        info = body.get("jobPostingInfo")
        if not isinstance(info, dict):
            raise TypeError("jobPostingInfo is missing")
        provider_id = info.get("jobReqId")
        title = info.get("title")
        external_path = info.get("externalPath")
        external_url = info.get("externalUrl")
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ValueError("jobReqId is missing")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title is missing")
        # CXS search rows carry externalPath. The detail payload observed on
        # production boards does not; when a future detail response does supply
        # it, retain the cross-endpoint consistency check.
        if external_path is not None and external_path != expected_path:
            raise ValueError("detail externalPath does not match list path")
        if not isinstance(external_url, str) or not self._http_url(external_url):
            raise ValueError("externalUrl is missing or invalid")
        description_html = (
            info.get("jobDescription") if isinstance(info.get("jobDescription"), str) else None
        )
        description = html_to_text(description_html)
        company = self._descriptor(info.get("hiringOrganization")) or target.company
        locations = self._locations(info)
        location = "; ".join(locations) or None
        normalized_location = normalize_location(location)
        countries = self._countries(info.get("country")) or set(normalized_location.countries)
        remote_value = info.get("remoteType")
        remote = (
            _REMOTE_TYPES.get(self._normalized_phrase(remote_value), RemoteStatus.UNKNOWN)
            if remote_value is not None
            else RemoteStatus.UNKNOWN
        )
        employment = _EMPLOYMENT_TYPES.get(self._normalized_phrase(info.get("timeType")))
        posted_at = self._date(info.get("startDate"))
        job_url = canonicalize_url(external_url)
        evidence = {
            "job_req_id": provider_id.strip(),
            "external_path": external_path,
            "external_url": external_url,
            "host": config.host,
            "tenant": config.tenant,
            "site": config.site,
            "configured_company": target.company,
            "hiring_organization": info.get("hiringOrganization"),
            "location": info.get("location"),
            "additional_locations": info.get("additionalLocations"),
            "country": info.get("country"),
            "remote_type": remote_value,
            "time_type": info.get("timeType"),
            "start_date": info.get("startDate"),
            "posted": info.get("posted"),
            "posted_on": info.get("postedOn"),
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "source_facts": evidence,
                    "description": description,
                    "countries": sorted(countries),
                    "remote": remote.value,
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        return Job(
            id=str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"workday:{config.board_id}:{provider_id.strip()}")
            ),
            source=self.source,
            source_job_id=provider_id.strip(),
            source_board_id=config.board_id,
            title=title,
            company=company,
            description_text=description,
            description_html=description_html,
            job_url=job_url,
            apply_url=None,
            canonical_url=job_url,
            location_text=location,
            country=next(iter(countries)) if len(countries) == 1 else None,
            eligible_countries=countries,
            city=normalized_location.city,
            region=normalized_location.region,
            remote_status=remote,
            employment_type=employment,
            department=None,
            posted_at=posted_at,
            updated_at=None,
            content_fingerprint=fingerprint,
            raw_metadata=evidence,
        )

    @staticmethod
    def _locations(info: dict[str, Any]) -> list[str]:
        additional = info.get("additionalLocations")
        values = [info.get("location"), *(additional if isinstance(additional, list) else [])]
        locations = []
        for value in values:
            text = WorkdayCollector._descriptor(value)
            if text:
                locations.append(text)
        return list(dict.fromkeys(locations))

    @staticmethod
    def _countries(value: object) -> set[str]:
        text = WorkdayCollector._descriptor(value)
        if not text:
            return set()
        return {_COUNTRY_ALIASES.get(text.casefold(), text)}

    @staticmethod
    def _descriptor(value: object) -> str | None:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for key in ("descriptor", "name", "value"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
        return None

    @staticmethod
    def _normalized_phrase(value: object) -> str:
        return re.sub(r"[ _-]+", " ", value.strip().casefold()) if isinstance(value, str) else ""

    @staticmethod
    def _date(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value).replace(tzinfo=UTC)
        except ValueError:
            return None

    @staticmethod
    def _http_url(value: str) -> bool:
        parts = urlsplit(value)
        return parts.scheme in {"http", "https"} and bool(parts.netloc)

    def _pause(self) -> None:
        if self.delay > 0:
            time.sleep(self.delay)

    @staticmethod
    def _status_for_failure(failure: _RequestFailure) -> CollectionStatus:
        if failure.category == "network":
            return CollectionStatus.NETWORK_FAILURE
        if failure.status_code == 404:
            return CollectionStatus.INVALID_TARGET
        if failure.status_code == 429:
            return CollectionStatus.RATE_LIMITED
        if failure.status_code == 401:
            return CollectionStatus.AUTHENTICATION_FAILURE
        if failure.status_code == 403:
            return CollectionStatus.FORBIDDEN
        if failure.status_code is not None and 500 <= failure.status_code < 600:
            return CollectionStatus.PROVIDER_ERROR
        return CollectionStatus.PARSE_FAILURE

    def _failure(
        self, target: SourceTarget, status: CollectionStatus, message: str
    ) -> CollectionResult:
        return CollectionResult(source=self.source, target=target, status=status, errors=[message])
