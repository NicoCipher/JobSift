from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError

from job_scout.domain.models import (
    CollectionResult,
    CollectionStatus,
    EmploymentType,
    Job,
    SourceTarget,
)
from job_scout.incremental_collection import IncrementalTargetState
from job_scout.normalization.core import (
    canonicalize_url,
    classify_remote,
    content_fingerprint,
    html_to_text,
)
from job_scout.normalization.location import normalize_location


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
    metadata: Any = None


class _Payload(BaseModel):
    jobs: list[Any]


class GreenhouseCollector:
    source = "greenhouse"
    base_url = "https://boards-api.greenhouse.io/v1/boards"
    incremental_overlap = timedelta(hours=26)

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        incremental_states: list[IncrementalTargetState] | None = None,
        incremental_detail_limit: int = 200,
    ) -> None:
        if incremental_detail_limit < 1:
            raise ValueError("incremental_detail_limit must be at least 1")
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(15.0),
            headers={"User-Agent": "JobSift/0.1 (+https://github.com/NicoCipher/JobSift)"},
            transport=httpx.HTTPTransport(retries=2),
        )
        self.incremental_detail_limit = incremental_detail_limit
        self.incremental_states = {
            state.board_id.casefold(): state
            for state in (incremental_states or [])
            if state.source == self.source
        }
        self.incremental_enabled = incremental_states is not None
        self.last_counts: dict[str, int] = {}

    def collect(self, target: SourceTarget) -> CollectionResult:
        board = target.board_id.strip()
        url = f"{self.base_url}/{board}/jobs"
        state = self.incremental_states.get(board.casefold()) if self.incremental_enabled else None
        known_ids = set(state.known_source_job_ids) if state is not None else set()
        active_posted = (
            state.active_posted_at_by_source_job_id if state is not None else {}
        )
        self.last_counts = {
            "raw_received": 0,
            "normalized": 0,
            "detail_requests": 0,
            "detail_budget_used": 0,
            "incremental_known_ids": len(known_ids),
            "incremental_new_ids": 0,
            "reused_posted_at": 0,
            "suppressed_known_stale": 0,
            "suppressed_known_unknown_age": 0,
            "detail_deferred": 0,
            "vanished": 0,
            "quarantined": 0,
        }
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
            self.last_counts["raw_received"] = len(payload.jobs)
            jobs: list[Job] = []
            errors: list[str] = []
            terminal_status: CollectionStatus | None = None
            for index, raw_job in enumerate(payload.jobs):
                try:
                    item = _GreenhouseJob.model_validate(raw_job)
                    source_job_id = str(item.id)
                    if state is None:
                        jobs.append(self._normalize(item, target))
                        continue

                    previous_posted_at = active_posted.get(source_job_id)
                    if item.first_published is None and previous_posted_at is not None:
                        item = item.model_copy(update={"first_published": previous_posted_at})
                        self.last_counts["reused_posted_at"] += 1

                    if item.first_published is not None:
                        if (
                            source_job_id in known_ids
                            and self._outside_incremental_overlap(item.first_published)
                        ):
                            self.last_counts["suppressed_known_stale"] += 1
                            continue
                        jobs.append(self._normalize(item, target))
                        continue

                    if source_job_id in known_ids:
                        self.last_counts["suppressed_known_unknown_age"] += 1
                        continue

                    self.last_counts["incremental_new_ids"] += 1
                    if self.last_counts["detail_budget_used"] >= self.incremental_detail_limit:
                        self.last_counts["detail_deferred"] += 1
                        continue

                    self.last_counts["detail_requests"] += 1
                    detail_response = self.client.get(f"{url}/{item.id}")
                    if detail_response.status_code in {404, 410}:
                        # Vanished postings are terminal evidence for this index row.
                        # They must not consume the hydration cap, otherwise a run can
                        # retry the same leading 404/410 rows forever and never reach
                        # later unseen postings.
                        self.last_counts["vanished"] += 1
                        continue

                    self.last_counts["detail_budget_used"] += 1
                    if detail_response.status_code == 429:
                        errors.append(f"detail[{item.id}] HTTP 429")
                        terminal_status = CollectionStatus.RATE_LIMITED
                        break
                    if detail_response.status_code == 401:
                        errors.append(f"detail[{item.id}] HTTP 401")
                        terminal_status = CollectionStatus.AUTHENTICATION_FAILURE
                        break
                    if detail_response.status_code == 403:
                        throttled = "retry-after" in {
                            key.casefold() for key in detail_response.headers
                        } or any(
                            marker in detail_response.text.casefold()
                            for marker in ("rate limit", "throttle")
                        )
                        errors.append(f"detail[{item.id}] HTTP 403")
                        terminal_status = (
                            CollectionStatus.RATE_LIMITED
                            if throttled
                            else CollectionStatus.FORBIDDEN
                        )
                        break
                    if detail_response.status_code >= 500:
                        errors.append(
                            f"detail[{item.id}] HTTP {detail_response.status_code}"
                        )
                        terminal_status = CollectionStatus.PROVIDER_ERROR
                        break
                    detail_response.raise_for_status()
                    try:
                        detail = _GreenhouseJob.model_validate(detail_response.json())
                        if detail.id != item.id:
                            raise ValueError(
                                "hydrated posting id does not match indexed posting"
                            )
                    except (ValueError, ValidationError) as exc:
                        # A provider row that consistently returns HTTP 200 but an
                        # unusable detail payload is terminal for freshness
                        # hydration. Persist the valid index identity with unknown
                        # age so downstream freshness still fails closed and the
                        # next incremental retry can advance past this row.
                        placeholder = self._normalize(item, target)
                        placeholder.raw_metadata["incremental_detail_quarantined"] = (
                            type(exc).__name__
                        )
                        jobs.append(placeholder)
                        errors.append(f"detail[{item.id}] rejected: {exc}")
                        self.last_counts["quarantined"] += 1
                        continue
                    jobs.append(self._normalize(detail, target))
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    errors.append(f"detail job[{index}] failed: {type(exc).__name__}")
                    terminal_status = CollectionStatus.NETWORK_FAILURE
                    break
                except (ValueError, ValidationError) as exc:
                    errors.append(f"job[{index}] rejected: {exc}")
                    self.last_counts["quarantined"] += 1
                except httpx.HTTPStatusError as exc:
                    errors.append(f"detail job[{index}] HTTP {exc.response.status_code}")
                    terminal_status = CollectionStatus.PROVIDER_ERROR
                    break
            if self.last_counts["detail_deferred"]:
                errors.append(
                    "incremental detail hydration cap reached; deferred unseen postings"
                )
            self.last_counts["normalized"] = len(jobs)
            return CollectionResult(
                source=self.source,
                target=target,
                status=(
                    terminal_status
                    if terminal_status is not None and not jobs
                    else CollectionStatus.PARTIAL
                    if errors
                    else CollectionStatus.SUCCESS
                ),
                jobs=jobs,
                errors=errors,
                raw_postings_received=len(payload.jobs),
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            return self._failure(target, CollectionStatus.NETWORK_FAILURE, type(exc).__name__)
        except (ValueError, ValidationError) as exc:
            return self._failure(target, CollectionStatus.PARSE_FAILURE, str(exc))
        except httpx.HTTPStatusError as exc:
            return self._failure(
                target, CollectionStatus.PROVIDER_ERROR, f"HTTP {exc.response.status_code}"
            )

    @classmethod
    def _outside_incremental_overlap(cls, posted_at: datetime) -> bool:
        if posted_at.tzinfo is None:
            return False
        return datetime.now(UTC) - posted_at.astimezone(UTC) > cls.incremental_overlap

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
        normalized_location = normalize_location(location, office_locations)
        employment_type, employment_evidence = self._employment_type_from_metadata(item.metadata)
        location_evidence = " | ".join(
            value for value in [location, *office_names, *office_locations] if value
        )
        raw_metadata = {
            "job_id": item.id,
            "board_token": target.board_id,
            "company": target.company,
            "employer_id": target.employer_id,
            "location": location,
            "departments": [department.name for department in item.departments],
            "offices": office_names,
            "office_locations": office_locations,
            "updated_at": item.updated_at.isoformat() if item.updated_at else None,
            "first_published": item.first_published.isoformat() if item.first_published else None,
            "absolute_url": str(item.absolute_url),
            "employment_type_evidence": employment_evidence,
        }
        return Job(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"greenhouse:{target.board_id}:{item.id}")),
            source=self.source,
            source_job_id=str(item.id),
            source_board_id=target.board_id,
            title=item.title,
            company=target.company,
            employer_id=target.employer_id,
            description_text=text,
            description_html=item.content,
            job_url=canonical,
            canonical_url=canonical,
            location_text=location,
            country=normalized_location.country,
            eligible_countries=set(normalized_location.countries),
            region=normalized_location.region,
            city=normalized_location.city,
            remote_status=classify_remote(location, text, office_names, office_locations),
            employment_type=employment_type,
            department=item.departments[0].name if item.departments else None,
            offices=office_names,
            posted_at=item.first_published,
            updated_at=item.updated_at,
            content_fingerprint=content_fingerprint(
                title=item.title,
                description=text,
                location=location_evidence or None,
                employment_type=employment_type,
            ),
            raw_metadata=raw_metadata,
        )

    @staticmethod
    def _employment_type_from_metadata(
        metadata: Any,
    ) -> tuple[EmploymentType | None, dict[str, str] | None]:
        if not isinstance(metadata, list):
            return None, None
        mapping = {
            "full time": EmploymentType.FULL_TIME,
            "part time": EmploymentType.PART_TIME,
            "contract": EmploymentType.CONTRACT,
            "temporary": EmploymentType.TEMPORARY,
            "internship": EmploymentType.INTERNSHIP,
            "freelance": EmploymentType.FREELANCE,
        }
        for field in metadata:
            if not isinstance(field, dict):
                continue
            name = re.sub(r"[_-]+", " ", str(field.get("name") or "")).casefold().strip()
            if name != "employment type":
                continue
            value = field.get("value")
            if not isinstance(value, str):
                return None, None
            normalized = re.sub(r"[_-]+", " ", value).casefold().strip()
            employment_type = mapping.get(normalized)
            if employment_type:
                return employment_type, {"field": str(field["name"]), "value": value}
        return None, None
