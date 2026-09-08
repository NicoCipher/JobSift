"""Public Lever postings collector backed by the validated list API only."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError

from job_scout.domain.models import (
    CollectionResult,
    CollectionStatus,
    EmploymentType,
    Job,
    LeverTargetConfig,
    RemoteStatus,
    SourceTarget,
)
from job_scout.normalization.core import canonicalize_url, html_to_text
from job_scout.normalization.location import normalize_location

PAGE_LIMIT = 50
RETRIES = 2
USER_AGENT = "JobSift/0.1 (+https://github.com/NicoCipher/JobSift)"
_API_HOSTS = {
    "global": "https://api.lever.co",
    "eu": "https://api.eu.lever.co",
}
_COUNTRY_CODES = {
    "AT": "Austria",
    "CA": "Canada",
    "DE": "Germany",
    "GB": "United Kingdom",
    "IE": "Ireland",
    "NG": "Nigeria",
    "SG": "Singapore",
    "US": "United States",
}
_WORKPLACE_TYPES = {
    "remote": RemoteStatus.REMOTE,
    "hybrid": RemoteStatus.HYBRID,
    "onsite": RemoteStatus.ONSITE,
}
_EMPLOYMENT_TYPES = {
    "full time": EmploymentType.FULL_TIME,
    "part time": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "temporary": EmploymentType.TEMPORARY,
    "internship": EmploymentType.INTERNSHIP,
    "freelance": EmploymentType.FREELANCE,
}


class _Categories(BaseModel):
    model_config = ConfigDict(extra="ignore")

    location: str | None = None
    allLocations: list[str] = Field(default_factory=list)
    commitment: str | None = None
    team: str | None = None
    department: str | None = None


class _LeverJob(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: Any = None
    text: str
    hostedUrl: HttpUrl
    applyUrl: HttpUrl
    categories: _Categories = Field(default_factory=_Categories)
    country: str | None = None
    workplaceType: str | None = None
    description: str | None = None
    descriptionPlain: str | None = None
    descriptionBodyPlain: str | None = None
    createdAt: Any = None
    salaryRange: Any = None


class LeverCollector:
    """Collect one explicit Lever instance/site using public list pages.

    The contract validation found no additional normalization evidence in the
    per-posting endpoint, so this collector intentionally makes no detail
    calls.
    """

    source = "lever"

    def __init__(self, client: httpx.Client | None = None, *, delay: float = 0.5) -> None:
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(20.0),
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            transport=httpx.HTTPTransport(retries=RETRIES),
        )
        self.delay = delay
        self.last_counts: dict[str, int | str] = {}

    def collect(self, target: SourceTarget) -> CollectionResult:
        self.last_counts = {"pages": 0, "received": 0, "normalized": 0, "quarantined": 0}
        config, configuration_error = self._configuration(target)
        if configuration_error:
            return self._failure(target, CollectionStatus.INVALID_TARGET, configuration_error)
        assert config is not None

        jobs: list[Job] = []
        errors: list[str] = []
        signatures: set[str] = set()
        provider_ids: set[str] = set()
        offset = 0
        try:
            while True:
                response = self._request_page(config, offset)
                if response.status_code == 404:
                    return self._failure(target, CollectionStatus.INVALID_TARGET, "HTTP 404")
                if response.status_code == 401:
                    return self._failure(
                        target, CollectionStatus.AUTHENTICATION_FAILURE, "HTTP 401"
                    )
                if response.status_code == 403:
                    throttled = "retry-after" in response.headers or any(
                        marker in response.text.casefold() for marker in ("rate limit", "throttle")
                    )
                    return self._failure(
                        target,
                        CollectionStatus.RATE_LIMITED if throttled else CollectionStatus.FORBIDDEN,
                        "HTTP 403",
                    )
                if response.status_code == 429:
                    return self._failure(target, CollectionStatus.RATE_LIMITED, "HTTP 429")
                response.raise_for_status()
                try:
                    page = response.json()
                except ValueError as exc:
                    return self._failure(target, CollectionStatus.PARSE_FAILURE, type(exc).__name__)
                if not isinstance(page, list):
                    return self._failure(
                        target, CollectionStatus.PARSE_FAILURE, "top-level payload is not a list"
                    )

                signature = self._page_signature(page)
                if signature in signatures:
                    errors.append(f"offset {offset}: repeated page signature")
                    break
                signatures.add(signature)
                self.last_counts["pages"] = int(self.last_counts["pages"]) + 1
                self.last_counts["received"] = int(self.last_counts["received"]) + len(page)

                for index, raw in enumerate(page):
                    try:
                        job = self._normalize(_LeverJob.model_validate(raw), target, config)
                        if job.source_job_id in provider_ids:
                            raise ValueError("duplicate provider id in board response")
                        provider_ids.add(job.source_job_id)
                        jobs.append(job)
                    except (ValueError, ValidationError) as exc:
                        errors.append(f"offset {offset} job[{index}] rejected: {exc}")

                if len(page) < PAGE_LIMIT:
                    break
                offset += PAGE_LIMIT
                self._pause(1)
        except httpx.RequestError as exc:
            return self._failure(target, CollectionStatus.NETWORK_FAILURE, type(exc).__name__)
        except httpx.HTTPStatusError as exc:
            return self._failure(
                target, CollectionStatus.PROVIDER_ERROR, f"HTTP {exc.response.status_code}"
            )

        self.last_counts.update(normalized=len(jobs), quarantined=len(errors))
        return CollectionResult(
            source=self.source,
            target=target,
            status=CollectionStatus.PARTIAL if errors else CollectionStatus.SUCCESS,
            jobs=jobs,
            errors=errors,
        )

    def _configuration(self, target: SourceTarget) -> tuple[LeverTargetConfig | None, str | None]:
        config = target.lever
        if config is None:
            return None, "Lever requires explicit instance and site configuration"
        if target.board_id != config.board_id:
            return None, "board_id must equal explicit Lever instance:site configuration"
        return config, None

    @staticmethod
    def _base(config: LeverTargetConfig) -> str:
        return f"{_API_HOSTS[config.instance]}/v0/postings/{quote(config.site, safe='')}"

    def _request_page(self, config: LeverTargetConfig, offset: int) -> httpx.Response:
        for attempt in range(RETRIES + 1):
            try:
                response = self.client.get(
                    self._base(config),
                    params={"mode": "json", "skip": offset, "limit": PAGE_LIMIT},
                )
            except httpx.RequestError:
                if attempt == RETRIES:
                    raise
                self._pause(attempt + 1)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == RETRIES:
                    return response
                self._pause(attempt + 1)
                continue
            return response
        raise AssertionError("unreachable")

    def _pause(self, multiplier: int) -> None:
        if self.delay > 0:
            time.sleep(self.delay * multiplier)

    @staticmethod
    def _page_signature(page: list[Any]) -> str:
        return hashlib.sha256(
            json.dumps(page, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()

    def _normalize(self, item: _LeverJob, target: SourceTarget, config: LeverTargetConfig) -> Job:
        provider_id = self._provider_id(item.id)
        description = self._description(item)
        location_values = self._locations(item.categories)
        location = "; ".join(location_values) or None
        normalized_location = normalize_location(location, location_values)
        countries = self._countries(item.country, normalized_location.countries)
        employment_type = self._employment_type(item.categories.commitment)
        remote_status = _WORKPLACE_TYPES.get(
            (item.workplaceType or "").strip().casefold(), RemoteStatus.UNKNOWN
        )
        job_url = canonicalize_url(str(item.hostedUrl))
        apply_url = canonicalize_url(str(item.applyUrl))
        raw_metadata = {
            "instance": config.instance,
            "site": config.site,
            "provider_id": provider_id,
            "board_token": target.board_id,
            "company": target.company,
            "location": item.categories.location,
            "allLocations": item.categories.allLocations,
            "country": item.country,
            "workplaceType": item.workplaceType,
            "commitment": item.categories.commitment,
            "team": item.categories.team,
            "department": item.categories.department,
            "hostedUrl": str(item.hostedUrl),
            "applyUrl": str(item.applyUrl),
            "createdAt": item.createdAt,
            "salaryRange": item.salaryRange,
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "source_facts": raw_metadata,
                    "title": item.text,
                    "description": description,
                    "countries": sorted(countries),
                    "remote": remote_status.value,
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        return Job(
            id=str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL, f"lever:{config.instance}:{config.site}:{provider_id}"
                )
            ),
            source=self.source,
            source_job_id=provider_id,
            source_board_id=config.board_id,
            title=item.text,
            company=target.company,
            description_text=description,
            description_html=item.description,
            job_url=job_url,
            apply_url=apply_url,
            canonical_url=job_url,
            location_text=location,
            country=next(iter(countries)) if len(countries) == 1 else None,
            eligible_countries=countries,
            region=normalized_location.region,
            city=normalized_location.city,
            remote_status=remote_status,
            employment_type=employment_type,
            department=item.categories.department,
            offices=location_values,
            posted_at=None,
            updated_at=None,
            content_fingerprint=fingerprint,
            raw_metadata=raw_metadata,
        )

    @staticmethod
    def _provider_id(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("provider id is missing")
        return value.strip()

    @staticmethod
    def _description(item: _LeverJob) -> str | None:
        for value in (item.descriptionPlain, item.descriptionBodyPlain):
            if value and value.strip():
                return value.strip()
        return html_to_text(item.description)

    @staticmethod
    def _locations(categories: _Categories) -> list[str]:
        values = [categories.location, *categories.allLocations]
        return list(dict.fromkeys(value.strip() for value in values if value and value.strip()))

    @staticmethod
    def _countries(country: str | None, inferred: set[str] | frozenset[str]) -> set[str]:
        if country and country.strip():
            mapped = _COUNTRY_CODES.get(country.strip().upper())
            return {mapped} if mapped else set()
        return set(inferred)

    @staticmethod
    def _employment_type(value: str | None) -> EmploymentType | None:
        if not value:
            return None
        normalized = re.sub(r"[-_]+", " ", value).casefold().strip()
        return _EMPLOYMENT_TYPES.get(normalized)

    def _failure(
        self, target: SourceTarget, status: CollectionStatus, message: str
    ) -> CollectionResult:
        return CollectionResult(source=self.source, target=target, status=status, errors=[message])
