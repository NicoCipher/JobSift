from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError

from job_scout.domain.models import CollectionResult, CollectionStatus, Job, SourceTarget
from job_scout.normalization.core import (
    canonicalize_url,
    classify_remote,
    content_fingerprint,
    html_to_text,
)


class _Location(BaseModel):
    name: str | None = None


class _Department(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str


class _Office(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str | None = None
    location: str | None = None


class _GreenhouseJob(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    title: str
    location: _Location | None = None
    absolute_url: HttpUrl
    content: str | None = None
    updated_at: datetime | None = None
    first_published: datetime | None = None
    departments: list[_Department] = Field(default_factory=list)
    offices: list[_Office] = Field(default_factory=list)


class _Payload(BaseModel):
    jobs: list[Any]


class GreenhouseCollector:
    source = "greenhouse"
    base_url = "https://boards-api.greenhouse.io/v1/boards"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(15.0),
            headers={"User-Agent": "JobSift/0.1 (+https://github.com/NicoCipher/JobSift)"},
            transport=httpx.HTTPTransport(retries=2),
        )

    def collect(self, target: SourceTarget) -> CollectionResult:
        url = f"{self.base_url}/{target.board_id}/jobs"
        try:
            response = self.client.get(url, params={"content": "true"})
            if response.status_code == 404:
                return self._failure(target, CollectionStatus.INVALID_TARGET, "board not found")
            if response.status_code == 429:
                return self._failure(
                    target, CollectionStatus.RATE_LIMITED, "provider rate limited request"
                )
            if response.status_code == 403:
                throttled = "retry-after" in {key.casefold() for key in response.headers} or any(
                    marker in response.text.casefold() for marker in ("rate limit", "throttle")
                )
                status = CollectionStatus.RATE_LIMITED if throttled else CollectionStatus.FORBIDDEN
                return self._failure(target, status, "HTTP 403")
            if response.status_code == 401:
                return self._failure(
                    target, CollectionStatus.AUTHENTICATION_FAILURE, f"HTTP {response.status_code}"
                )
            if response.status_code >= 500:
                return self._failure(
                    target, CollectionStatus.PROVIDER_ERROR, f"HTTP {response.status_code}"
                )
            response.raise_for_status()
            payload = _Payload.model_validate(response.json())
            jobs: list[Job] = []
            errors: list[str] = []
            for index, raw_job in enumerate(payload.jobs):
                try:
                    jobs.append(self._normalize(_GreenhouseJob.model_validate(raw_job), target))
                except (ValueError, ValidationError) as exc:
                    errors.append(f"job[{index}] rejected: {exc}")
            return CollectionResult(
                source=self.source,
                target=target,
                status=CollectionStatus.PARTIAL if errors else CollectionStatus.SUCCESS,
                jobs=jobs,
                errors=errors,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            return self._failure(target, CollectionStatus.NETWORK_FAILURE, type(exc).__name__)
        except (ValueError, ValidationError) as exc:
            return self._failure(target, CollectionStatus.PARSE_FAILURE, str(exc))
        except httpx.HTTPStatusError as exc:
            return self._failure(
                target, CollectionStatus.PROVIDER_ERROR, f"HTTP {exc.response.status_code}"
            )

    def _failure(
        self, target: SourceTarget, status: CollectionStatus, message: str
    ) -> CollectionResult:
        return CollectionResult(source=self.source, target=target, status=status, errors=[message])

    def _normalize(self, item: _GreenhouseJob, target: SourceTarget) -> Job:
        text = html_to_text(item.content)
        canonical = canonicalize_url(str(item.absolute_url))
        location = item.location.name if item.location else None
        office_names = [office.name for office in item.offices if office.name]
        office_locations = [office.location for office in item.offices if office.location]
        raw_metadata = {
            "job_id": item.id,
            "board_token": target.board_id,
            "company": target.company,
            "location": location,
            "departments": [department.name for department in item.departments],
            "offices": office_names,
            "office_locations": office_locations,
            "updated_at": item.updated_at.isoformat() if item.updated_at else None,
            "first_published": item.first_published.isoformat() if item.first_published else None,
            "absolute_url": str(item.absolute_url),
        }
        return Job(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"greenhouse:{target.board_id}:{item.id}")),
            source=self.source,
            source_job_id=str(item.id),
            source_board_id=target.board_id,
            title=item.title,
            company=target.company,
            description_text=text,
            description_html=item.content,
            job_url=canonical,
            canonical_url=canonical,
            location_text=location,
            remote_status=classify_remote(location, text),
            department=item.departments[0].name if item.departments else None,
            offices=office_names,
            posted_at=item.first_published,
            updated_at=item.updated_at,
            content_fingerprint=content_fingerprint(
                title=item.title, description=text, location=location, employment_type=None
            ),
            raw_metadata=raw_metadata,
        )
