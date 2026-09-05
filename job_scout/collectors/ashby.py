from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StrictBool, ValidationError

from job_scout.domain.models import (
    CollectionResult,
    CollectionStatus,
    EmploymentType,
    Job,
    RemoteStatus,
    SourceTarget,
)
from job_scout.normalization.core import canonicalize_url, classify_remote, html_to_text
from job_scout.normalization.location import normalize_location


class _PostalAddress(BaseModel):
    model_config = ConfigDict(extra="ignore")
    addressCountry: str | None = None
    addressRegion: str | None = None
    addressLocality: str | None = None


class _Address(_PostalAddress):
    # The public documentation uses flat secondary addresses; preserved payloads
    # also contain the primary-style wrapper on secondary locations.
    postalAddress: _PostalAddress | None = None

    def postal(self) -> _PostalAddress:
        return self.postalAddress if self.postalAddress is not None else self


class _SecondaryLocation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    location: str | None = None
    address: _Address | None = None


class _AshbyJob(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: Any = None
    title: str
    location: str | None = None
    secondaryLocations: list[_SecondaryLocation] = Field(default_factory=list)
    address: _Address | None = None
    department: str | None = None
    team: str | None = None
    isListed: StrictBool | None = None
    isRemote: StrictBool | None = None
    workplaceType: str | None = None
    descriptionPlain: str | None = None
    descriptionHtml: str | None = None
    publishedAt: datetime | None = None
    employmentType: str | None = None
    jobUrl: HttpUrl
    applyUrl: HttpUrl | None = None


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    apiVersion: str | None = None
    jobs: list[Any]


# Explicit structured country names/codes evidenced in the first cohort, plus
# the existing Nigeria client market. This is not location geocoding.
_COUNTRY_CODES = {
    "United States": ("US", "USA", "U.S.", "U.S.A.", "United States of America"),
    "United Kingdom": ("UK", "GB", "GBR", "U.K."),
    "Nigeria": ("NG", "NGA"),
    "India": ("IN", "IND"),
    "Canada": ("CA", "CAN"),
    "Italy": ("IT", "ITA"),
    "Luxembourg": ("LU", "LUX"),
    "Ireland": ("IE", "IRL"),
    "Spain": ("ES", "ESP"),
    "Netherlands": ("NL", "NLD"),
    "Germany": ("DE", "DEU"),
    "Denmark": ("DK", "DNK"),
    "Portugal": ("PT", "PRT"),
    "France": ("FR", "FRA"),
    "Singapore": ("SG", "SGP"),
    "Australia": ("AU", "AUS"),
    "Japan": ("JP", "JPN"),
    "Poland": ("PL", "POL"),
    "Austria": ("AT", "AUT"),
    "Israel": ("IL", "ISR"),
}
_COUNTRY_ALIASES = {
    alias.casefold(): country
    for country, aliases in _COUNTRY_CODES.items()
    for alias in (country, *aliases)
}
_EMPLOYMENT = {
    "FullTime": EmploymentType.FULL_TIME,
    "PartTime": EmploymentType.PART_TIME,
    "Contract": EmploymentType.CONTRACT,
    "Temporary": EmploymentType.TEMPORARY,
    "Intern": EmploymentType.INTERNSHIP,
}
_WORKPLACE = {
    "Remote": RemoteStatus.REMOTE,
    "Hybrid": RemoteStatus.HYBRID,
    "OnSite": RemoteStatus.ONSITE,
}


def _structured_countries(address: _Address | None) -> set[str]:
    if address is None:
        return set()
    value = (address.postal().addressCountry or "").strip()
    country = _COUNTRY_ALIASES.get(value.casefold())
    if country:
        return {country}
    # Mixed evidence such as 'US | EU' contributes US only; EU is not a country.
    explicit = set(normalize_location(value).countries)
    if explicit:
        return explicit
    regions = {
        "european union",
        "europe",
        "africa",
        "asia",
        "north america",
        "south america",
        "latin america",
        "americas",
        "global",
        "worldwide",
        "remote",
        "international",
        "anywhere",
        "unknown",
        "amer",
        "emea",
        "apac",
        "latam",
        "mena",
    }
    # A named country supplied in the structured country field is direct evidence.
    # Retain its source spelling. Unrecognized short codes and regional labels are
    # not expanded into invented country names.
    if (
        len(value) > 3
        and value.casefold() not in regions
        and re.fullmatch(r"[^\W\d_]+(?:[ '-][^\W\d_]+)*", value)
    ):
        return {value}
    return set()


def _identity(item: _AshbyJob, board: str) -> tuple[str, str]:
    if isinstance(item.id, str) and re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", item.id.strip()
    ):
        identifier = item.id.strip()
        try:
            identifier = str(uuid.UUID(identifier))
        except ValueError:
            pass
        return identifier, "provider_id"
    parts = urlsplit(str(item.jobUrl))
    segments = parts.path.strip("/").split("/")
    if parts.hostname == "jobs.ashbyhq.com" and len(segments) == 2 and segments[0] == board:
        try:
            return str(uuid.UUID(segments[1])), "job_url"
        except ValueError:
            pass
    raise ValueError(
        "missing stable posting identity: provider id or board-scoped Ashby UUID URL required"
    )


class AshbyCollector:
    source = "ashby"
    base_url = "https://api.ashbyhq.com/posting-api/job-board"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(15.0),
            headers={"User-Agent": "JobSift/0.1 (+https://github.com/NicoCipher/JobSift)"},
            transport=httpx.HTTPTransport(retries=2),
        )
        self.last_counts: dict[str, int] = {}

    def collect(self, target: SourceTarget) -> CollectionResult:
        self.last_counts = {"received": 0, "skipped_unlisted": 0, "normalized": 0, "quarantined": 0}
        if not target.board_id.strip():
            return self._failure(target, CollectionStatus.INVALID_TARGET, "board name is blank")
        try:
            response = self.client.get(
                f"{self.base_url}/{quote(target.board_id, safe='')}",
                params={"includeCompensation": "false"},
            )
            status = response.status_code
            failures = {
                404: CollectionStatus.INVALID_TARGET,
                429: CollectionStatus.RATE_LIMITED,
                401: CollectionStatus.AUTHENTICATION_FAILURE,
            }
            if status in failures:
                return self._failure(target, failures[status], f"HTTP {status}")
            if status == 403:
                throttled = "retry-after" in response.headers or any(
                    marker in response.text.casefold() for marker in ("rate limit", "throttle")
                )
                return self._failure(
                    target,
                    CollectionStatus.RATE_LIMITED if throttled else CollectionStatus.FORBIDDEN,
                    "HTTP 403",
                )
            response.raise_for_status()
            payload = _Payload.model_validate(response.json())
            self.last_counts["received"] = len(payload.jobs)
            jobs = []
            errors = []
            for index, raw in enumerate(payload.jobs):
                # An unlisted item need not satisfy the discoverable-job schema.
                if isinstance(raw, dict) and raw.get("isListed") is False:
                    self.last_counts["skipped_unlisted"] += 1
                    continue
                try:
                    jobs.append(
                        self._normalize(_AshbyJob.model_validate(raw), target, payload.apiVersion)
                    )
                except ValidationError as exc:
                    errors.append(
                        f"job[{index}] rejected: {exc.errors(include_input=False, include_url=False)}"
                    )
                except ValueError as exc:
                    errors.append(f"job[{index}] rejected: {exc}")
            self.last_counts.update(normalized=len(jobs), quarantined=len(errors))
            return CollectionResult(
                source=self.source,
                target=target,
                jobs=jobs,
                errors=errors,
                status=CollectionStatus.PARTIAL if errors else CollectionStatus.SUCCESS,
            )
        except httpx.RequestError as exc:
            return self._failure(target, CollectionStatus.NETWORK_FAILURE, type(exc).__name__)
        except httpx.HTTPStatusError as exc:
            return self._failure(
                target, CollectionStatus.PROVIDER_ERROR, f"HTTP {exc.response.status_code}"
            )
        except (ValueError, ValidationError) as exc:
            return self._failure(target, CollectionStatus.PARSE_FAILURE, type(exc).__name__)

    def _failure(
        self, target: SourceTarget, status: CollectionStatus, message: str
    ) -> CollectionResult:
        return CollectionResult(source=self.source, target=target, status=status, errors=[message])

    def _normalize(self, item: _AshbyJob, target: SourceTarget, api_version: str | None) -> Job:
        source_id, identity_source = _identity(item, target.board_id)
        plain = item.descriptionPlain
        description = plain if plain and plain.strip() else html_to_text(item.descriptionHtml)
        primary = normalize_location(item.location)
        countries = _structured_countries(item.address) or set(primary.countries)
        locations = [item.location] if item.location else []
        for secondary in item.secondaryLocations:
            countries.update(
                _structured_countries(secondary.address)
                or normalize_location(secondary.location).countries
            )
            if secondary.location:
                locations.append(secondary.location)
        location = "; ".join(dict.fromkeys(locations)) or None
        if item.workplaceType is not None:
            remote = _WORKPLACE.get(item.workplaceType, RemoteStatus.UNKNOWN)
        elif item.isRemote is True:
            remote = RemoteStatus.REMOTE
        elif item.isRemote is None:
            remote = classify_remote(location, description)
        else:
            remote = RemoteStatus.UNKNOWN
        postal = item.address.postal() if item.address else None
        job_url = canonicalize_url(str(item.jobUrl))
        apply_url = canonicalize_url(str(item.applyUrl)) if item.applyUrl else None
        evidence = item.model_dump(
            mode="json", exclude={"descriptionPlain", "descriptionHtml", "id"}
        )
        evidence.update(
            provider_id=item.id if isinstance(item.id, str) else None,
            board_token=target.board_id,
            company=target.company,
            api_version=api_version,
            identity_source=identity_source,
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "source_facts": evidence,
                    "description": description,
                    "countries": sorted(countries),
                    "remote": remote.value,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return Job(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"ashby:{target.board_id}:{source_id}")),
            source=self.source,
            source_board_id=target.board_id,
            source_job_id=source_id,
            title=item.title,
            company=target.company,
            description_text=description,
            description_html=item.descriptionHtml,
            job_url=job_url,
            apply_url=apply_url,
            canonical_url=job_url,
            location_text=location,
            country=next(iter(countries)) if len(countries) == 1 else None,
            eligible_countries=countries,
            city=(postal.addressLocality if postal else None) or primary.city,
            region=(postal.addressRegion if postal else None) or primary.region,
            remote_status=remote,
            employment_type=_EMPLOYMENT.get(item.employmentType),
            department=item.department,
            posted_at=item.publishedAt,
            updated_at=None,
            content_fingerprint=fingerprint,
            raw_metadata=evidence,
        )
