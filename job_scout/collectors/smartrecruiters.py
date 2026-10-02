from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError, field_validator

from job_scout.domain.models import (
    CollectionResult,
    CollectionStatus,
    EmploymentType,
    Job,
    RemoteStatus,
    SourceTarget,
)
from job_scout.normalization.core import canonicalize_url, content_fingerprint, html_to_text
from job_scout.normalization.country_codes import country_name


class _Company(BaseModel):
    model_config = ConfigDict(extra="ignore")
    identifier: str | None = None
    name: str | None = None


class _Location(BaseModel):
    model_config = ConfigDict(extra="ignore")
    city: str | None = None
    region: str | None = None
    country: str | None = None
    remote: bool | None = None
    locationType: str | None = None


class _Label(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str | int | None = None
    label: str | None = None
    description: str | None = None


class _Posting(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    uuid: str | None = None
    name: str
    refNumber: str | None = None
    company: _Company | None = None
    releasedDate: datetime | None = None
    location: _Location | None = None
    department: _Label | None = None
    function: _Label | None = None
    typeOfEmployment: _Label | None = None
    experienceLevel: _Label | None = None
    ref: HttpUrl | None = None

    @field_validator("releasedDate")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        return value if value is not None and value.tzinfo is not None else None


class _Section(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str | None = None
    text: str | None = None


class _Sections(BaseModel):
    model_config = ConfigDict(extra="ignore")
    companyDescription: _Section | None = None
    jobDescription: _Section | None = None
    qualifications: _Section | None = None
    additionalInformation: _Section | None = None


class _JobAd(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sections: _Sections | None = None


class _PostingDetails(_Posting):
    postingUrl: HttpUrl
    applyUrl: HttpUrl | None = None
    active: bool | None = None
    jobAd: _JobAd | None = None


class _ListResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    limit: int = Field(ge=0)
    offset: int = Field(ge=0)
    totalFound: int = Field(ge=0)
    content: list[Any]


_EMPLOYMENT = {
    "full time": EmploymentType.FULL_TIME,
    "part time": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "temporary": EmploymentType.TEMPORARY,
    "internship": EmploymentType.INTERNSHIP,
    "intern": EmploymentType.INTERNSHIP,
    "freelance": EmploymentType.FREELANCE,
}

_WORKPLACE = {
    "remote": RemoteStatus.REMOTE,
    "hybrid": RemoteStatus.HYBRID,
    "onsite": RemoteStatus.ONSITE,
    "on site": RemoteStatus.ONSITE,
}


def _normalized_label(value: str | None) -> str:
    return re.sub(r"[-_]+", " ", value or "").casefold().strip()


class SmartRecruitersCollector:
    """Collect public SmartRecruiters postings and hydrate canonical job details."""

    source = "smartrecruiters"
    base_url = "https://api.smartrecruiters.com/v1/companies"
    page_size = 100

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        max_pages: int = 50,
        max_postings: int = 5000,
        title_filter: Callable[[str], bool] | None = None,
    ) -> None:
        if max_pages < 1:
            raise ValueError("max_pages must be at least 1")
        if max_postings < 1:
            raise ValueError("max_postings must be at least 1")
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(15.0),
            headers={
                "Accept": "application/json",
                "User-Agent": "JobSift/0.1 (+https://github.com/NicoCipher/JobSift)",
            },
            transport=httpx.HTTPTransport(retries=2),
        )
        self.title_filter = title_filter
        self.max_pages = max_pages
        self.max_postings = max_postings
        self.last_counts: dict[str, int] = {}

    def collect(self, target: SourceTarget) -> CollectionResult:
        self.last_counts = {
            "list_requests": 0,
            "raw_received": 0,
            "provider_reported_total": 0,
            "detail_requests": 0,
            "indexed": 0,
            "hydrated": 0,
            "quarantined": 0,
            "vanished": 0,
            "index_timestamped": 0,
            "index_fresh_24h": 0,
            "plausible_index_matches": 0,
            "prefilter_suppressed": 0,
        }
        board = target.board_id.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", board):
            return self._failure(
                target,
                CollectionStatus.INVALID_TARGET,
                "invalid company identifier",
            )

        indexed: list[_Posting] = []
        errors: list[str] = []
        terminal_status: CollectionStatus | None = None
        total_found: int | None = None
        offset = 0
        seen_pages: set[tuple[str, ...]] = set()

        try:
            for _page in range(self.max_pages):
                self.last_counts["list_requests"] += 1
                response = self.client.get(
                    f"{self.base_url}/{board}/postings",
                    params={
                        "destination": "PUBLIC",
                        "limit": self.page_size,
                        "offset": offset,
                    },
                )
                failure = self._http_failure(target, response)
                if failure is not None:
                    if indexed:
                        errors.extend(failure.errors)
                        terminal_status = failure.status
                        break
                    return failure
                payload = _ListResult.model_validate(response.json())
                self.last_counts["raw_received"] += len(payload.content)
                self.last_counts["provider_reported_total"] = payload.totalFound
                consumed = payload.offset + len(payload.content)
                if payload.offset != offset or len(payload.content) > self.page_size:
                    raise ValueError("provider pagination coordinates do not match request")
                if consumed > payload.totalFound:
                    raise ValueError("index content exceeds provider totalFound")
                total_found = payload.totalFound if total_found is None else total_found
                if payload.totalFound != total_found:
                    errors.append("provider totalFound changed during pagination")
                    break

                page_items: list[_Posting] = []
                for index, raw in enumerate(payload.content):
                    try:
                        page_items.append(_Posting.model_validate(raw))
                    except (ValueError, ValidationError) as exc:
                        errors.append(f"posting[{offset + index}] rejected: {exc}")
                        self.last_counts["quarantined"] += 1
                signature = tuple(item.id for item in page_items)
                if signature and signature in seen_pages:
                    errors.append("repeated SmartRecruiters page signature")
                    break
                seen_pages.add(signature)
                remaining_capacity = self.max_postings - len(indexed)
                indexed.extend(page_items[:remaining_capacity])
                if len(indexed) >= self.max_postings and payload.totalFound > len(indexed):
                    errors.append(
                        "SmartRecruiters posting count exceeds configured hydration bound"
                    )
                    break

                if consumed == payload.totalFound:
                    break
                if not payload.content:
                    errors.append("pagination ended before totalFound was reached")
                    break
                offset = consumed
            else:
                if total_found is not None and len(indexed) < total_found:
                    errors.append("SmartRecruiters pagination exceeded configured page bound")
        except httpx.RequestError as exc:
            if not indexed:
                return self._failure(target, CollectionStatus.NETWORK_FAILURE, type(exc).__name__)
            errors.append(f"list pagination interrupted: {type(exc).__name__}")
            terminal_status = CollectionStatus.NETWORK_FAILURE
        except (ValueError, ValidationError) as exc:
            if not indexed:
                return self._failure(target, CollectionStatus.PARSE_FAILURE, str(exc))
            errors.append(f"list payload rejected: {exc}")
            terminal_status = CollectionStatus.PARSE_FAILURE
        except httpx.HTTPStatusError as exc:
            if not indexed:
                return self._failure(
                    target,
                    CollectionStatus.PROVIDER_ERROR,
                    f"HTTP {exc.response.status_code}",
                )
            errors.append(f"list pagination HTTP {exc.response.status_code}")
            terminal_status = CollectionStatus.PROVIDER_ERROR

        self.last_counts["indexed"] = len(indexed)
        jobs: list[Job] = []
        seen_postings: set[str] = set()
        for item in indexed:
            if item.id in seen_postings:
                continue
            seen_postings.add(item.id)
            if item.releasedDate is not None:
                self.last_counts["index_timestamped"] += 1
                age = (datetime.now(UTC) - item.releasedDate.astimezone(UTC)).total_seconds()
                self.last_counts["index_fresh_24h"] += int(-300 <= age <= 86400)
            if self.title_filter is not None and not self.title_filter(item.name):
                self.last_counts["prefilter_suppressed"] += 1
                continue
            self.last_counts["plausible_index_matches"] += 1
            try:
                self.last_counts["detail_requests"] += 1
                response = self.client.get(f"{self.base_url}/{board}/postings/{item.id}")
                if response.status_code in {404, 410}:
                    # Public indexes and detail reads are not atomic. A posting
                    # that closes between the two calls is no longer eligible,
                    # but it is not evidence that the company target is broken.
                    self.last_counts["vanished"] += 1
                    continue
                if response.status_code in {400, 422}:
                    errors.append(f"detail[{item.id}] HTTP {response.status_code}")
                    self.last_counts["quarantined"] += 1
                    continue
                failure = self._http_failure(target, response)
                if failure is not None:
                    errors.append(f"detail[{item.id}] {failure.errors[0]}")
                    self.last_counts["quarantined"] += 1
                    terminal_status = failure.status
                    break
                detail = _PostingDetails.model_validate(response.json())
                if detail.active is False:
                    self.last_counts["vanished"] += 1
                    continue
                detail = self._merge_index_evidence(detail, item)
                self._validate_detail_identity(detail, item, target)
                jobs.append(self._normalize(detail, target))
            except httpx.RequestError as exc:
                errors.append(f"detail[{item.id}] failed: {type(exc).__name__}")
                self.last_counts["quarantined"] += 1
                terminal_status = CollectionStatus.NETWORK_FAILURE
                break
            except (ValueError, ValidationError) as exc:
                errors.append(f"detail[{item.id}] rejected: {exc}")
                self.last_counts["quarantined"] += 1
            except httpx.HTTPStatusError as exc:
                errors.append(f"detail[{item.id}] HTTP {exc.response.status_code}")
                self.last_counts["quarantined"] += 1
                terminal_status = CollectionStatus.PROVIDER_ERROR
                break

        self.last_counts["hydrated"] = len(jobs)
        raw = self.last_counts["raw_received"]
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
            raw_postings_received=raw,
        )

    @staticmethod
    def _validate_detail_identity(
        detail: _PostingDetails,
        index: _Posting,
        target: SourceTarget,
    ) -> None:
        if detail.id != index.id:
            raise ValueError("hydrated posting id does not match indexed posting")
        if detail.uuid and index.uuid and detail.uuid.casefold() != index.uuid.casefold():
            raise ValueError("hydrated posting uuid does not match indexed posting")
        posting_url = urlsplit(str(detail.postingUrl))
        parts = posting_url.path.strip("/").split("/")
        if (
            posting_url.scheme != "https"
            or posting_url.hostname != "jobs.smartrecruiters.com"
            or posting_url.port not in {None, 443}
            or posting_url.username is not None
            or len(parts) != 2
            or parts[0].casefold() != target.board_id.casefold()
            or not (parts[1] == index.id or parts[1].startswith(index.id + "-"))
        ):
            raise ValueError("hydrated canonical URL does not match requested posting")
        if detail.applyUrl is not None:
            apply_url = urlsplit(str(detail.applyUrl))
            apply_parts = apply_url.path.strip("/").split("/")
            if (
                apply_url.scheme != "https"
                or apply_url.hostname != "jobs.smartrecruiters.com"
                or apply_url.port not in {None, 443}
                or apply_url.username is not None
                or apply_url.password is not None
                or len(apply_parts) not in {2, 3}
                or apply_parts[0].casefold() != target.board_id.casefold()
                or not (
                    apply_parts[1] == index.id
                    or apply_parts[1].startswith(index.id + "-")
                )
                or (
                    len(apply_parts) == 3
                    and apply_parts[2].casefold() != "apply"
                )
            ):
                raise ValueError(
                    "hydrated apply URL does not match requested posting"
                )
        identifier = detail.company.identifier if detail.company else None
        if identifier and identifier.casefold() != target.board_id.casefold():
            raise ValueError("hydrated posting company does not match requested target")

    @staticmethod
    def _merge_index_evidence(detail: _PostingDetails, index: _Posting) -> _PostingDetails:
        updates: dict[str, object] = {}
        for field in ("releasedDate", "uuid", "refNumber"):
            if getattr(detail, field) is None and getattr(index, field) is not None:
                updates[field] = getattr(index, field)

        # Detail payloads are authoritative; index data only fills fields the detail omitted.
        for field in (
            "location",
            "department",
            "function",
            "typeOfEmployment",
            "experienceLevel",
            "company",
        ):
            detail_value = getattr(detail, field)
            index_value = getattr(index, field)
            if detail_value is None:
                if index_value is not None:
                    updates[field] = index_value
                continue
            if index_value is None:
                continue
            missing = {
                nested_field: getattr(index_value, nested_field)
                for nested_field in type(detail_value).model_fields
                if getattr(detail_value, nested_field) is None
                and getattr(index_value, nested_field) is not None
            }
            if missing:
                updates[field] = detail_value.model_copy(update=missing)

        return detail.model_copy(update=updates) if updates else detail

    def _http_failure(
        self, target: SourceTarget, response: httpx.Response
    ) -> CollectionResult | None:
        status = response.status_code
        if status == 404:
            return self._failure(target, CollectionStatus.INVALID_TARGET, "HTTP 404")
        if status == 429:
            return self._failure(target, CollectionStatus.RATE_LIMITED, "HTTP 429")
        if status == 401:
            return self._failure(target, CollectionStatus.AUTHENTICATION_FAILURE, "HTTP 401")
        if status == 403:
            throttled = "retry-after" in {key.casefold() for key in response.headers} or any(
                marker in response.text.casefold() for marker in ("rate limit", "throttle")
            )
            return self._failure(
                target,
                CollectionStatus.RATE_LIMITED if throttled else CollectionStatus.FORBIDDEN,
                "HTTP 403",
            )
        if status >= 500:
            return self._failure(target, CollectionStatus.PROVIDER_ERROR, f"HTTP {status}")
        response.raise_for_status()
        return None

    def _failure(
        self, target: SourceTarget, status: CollectionStatus, message: str
    ) -> CollectionResult:
        return CollectionResult(
            source=self.source,
            target=target,
            status=status,
            errors=[message],
            raw_postings_received=self.last_counts.get("raw_received", 0),
        )

    @staticmethod
    def _description(item: _PostingDetails) -> tuple[str | None, str | None]:
        sections = item.jobAd.sections if item.jobAd else None
        if sections is None:
            return None, None
        values = [
            section.text
            for section in (
                sections.companyDescription,
                sections.jobDescription,
                sections.qualifications,
                sections.additionalInformation,
            )
            if section is not None and section.text and section.text.strip()
        ]
        if not values:
            return None, None
        html = "\n\n".join(values)
        return html_to_text(html), html

    @staticmethod
    def _employment(value: _Label | None) -> EmploymentType | None:
        if value is None:
            return None
        return _EMPLOYMENT.get(_normalized_label(value.label))

    @staticmethod
    def _location(item: _PostingDetails) -> tuple[str | None, str | None, str | None, str | None]:
        value = item.location
        if value is None:
            return None, None, None, None
        pieces = [
            part.strip()
            for part in (value.city, value.region, value.country)
            if part and part.strip()
        ]
        text = ", ".join(pieces) or None
        raw_country = value.country.strip() if value.country else None
        country = None
        if raw_country:
            country = country_name(raw_country)
        return text, country, value.region, value.city

    @staticmethod
    def _remote(item: _PostingDetails) -> RemoteStatus:
        location = item.location
        if location is None:
            return RemoteStatus.UNKNOWN
        workplace = _WORKPLACE.get(_normalized_label(location.locationType))
        if workplace is not None:
            return workplace
        if location.remote is True:
            return RemoteStatus.REMOTE
        return RemoteStatus.UNKNOWN

    def _normalize(self, item: _PostingDetails, target: SourceTarget) -> Job:
        description, description_html = self._description(item)
        location_text, country, region, city = self._location(item)
        employment = self._employment(item.typeOfEmployment)
        canonical = canonicalize_url(str(item.postingUrl))
        apply_url = canonicalize_url(str(item.applyUrl)) if item.applyUrl else None
        provider_uuid = item.uuid.strip() if item.uuid else None
        source_id = item.id
        raw_metadata = {
            "posting_id": item.id,
            "posting_uuid": provider_uuid,
            "ref_number": item.refNumber,
            "company_identifier": (
                item.company.identifier
                if item.company and item.company.identifier
                else target.board_id
            ),
            "company_name": item.company.name if item.company else None,
            "released_date": item.releasedDate.isoformat() if item.releasedDate else None,
            "location": item.location.model_dump(mode="json") if item.location else None,
            "department": item.department.model_dump(mode="json") if item.department else None,
            "function": item.function.model_dump(mode="json") if item.function else None,
            "type_of_employment": (
                item.typeOfEmployment.model_dump(mode="json") if item.typeOfEmployment else None
            ),
            "experience_level": (
                item.experienceLevel.model_dump(mode="json") if item.experienceLevel else None
            ),
            "posting_url": str(item.postingUrl),
            "apply_url": str(item.applyUrl) if item.applyUrl else None,
            "provider_contract": "smartrecruiters-posting-api-v1",
        }
        return Job(
            id=str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"smartrecruiters:{target.board_id.casefold()}:{source_id}",
                )
            ),
            source=self.source,
            source_job_id=source_id,
            source_board_id=target.board_id.casefold(),
            title=item.name,
            company=target.company,
            employer_id=target.employer_id,
            description_text=description,
            description_html=description_html,
            job_url=canonical,
            apply_url=apply_url,
            canonical_url=canonical,
            location_text=location_text,
            country=country,
            eligible_countries={country} if country else set(),
            region=region,
            city=city,
            remote_status=self._remote(item),
            employment_type=employment,
            department=item.department.label if item.department else None,
            posted_at=item.releasedDate,
            content_fingerprint=content_fingerprint(
                title=item.name,
                description=description,
                location=location_text,
                employment_type=employment,
            ),
            raw_metadata=raw_metadata,
        )
