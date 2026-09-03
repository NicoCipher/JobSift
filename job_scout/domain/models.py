from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class RemoteStatus(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    TEMPORARY = "temporary"
    INTERNSHIP = "internship"
    FREELANCE = "freelance"
    UNKNOWN = "unknown"


class Seniority(StrEnum):
    INTERN = "intern"
    JUNIOR = "junior"
    ENTRY = "entry"
    ASSOCIATE = "associate"
    MID = "mid"
    SENIOR = "senior"
    STAFF = "staff"
    PRINCIPAL = "principal"
    LEAD = "lead"
    MANAGER = "manager"
    DIRECTOR = "director"
    UNKNOWN = "unknown"


class MatchDecision(StrEnum):
    STRONG_MATCH = "strong_match"
    POSSIBLE_MATCH = "possible_match"
    REJECT = "reject"
    NEEDS_REVIEW = "needs_review"


class CollectionStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    RATE_LIMITED = "rate_limited"
    AUTHENTICATION_FAILURE = "authentication_failure"
    INVALID_TARGET = "invalid_target"
    PROVIDER_ERROR = "provider_error"
    NETWORK_FAILURE = "network_failure"
    PARSE_FAILURE = "parse_failure"


class JobLifecycle(StrEnum):
    NEW = "new"
    SEEN = "seen"
    CHANGED = "changed"
    POSSIBLY_CLOSED = "possibly_closed"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class Job(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    source: str
    source_job_id: str
    source_board_id: str
    title: str
    company: str
    description_text: str | None = None
    description_html: str | None = None
    job_url: HttpUrl
    apply_url: HttpUrl | None = None
    canonical_url: HttpUrl
    location_text: str | None = None
    country: str | None = None
    region: str | None = None
    city: str | None = None
    remote_status: RemoteStatus = RemoteStatus.UNKNOWN
    employment_type: EmploymentType | None = None
    seniority: Seniority | None = None
    department: str | None = None
    posted_at: datetime | None = None
    updated_at: datetime | None = None
    discovered_at: datetime = Field(default_factory=utc_now)
    last_seen_at: datetime = Field(default_factory=utc_now)
    content_fingerprint: str
    raw_metadata: dict[str, Any] = Field(default_factory=dict)
    discovered_via: str = "direct_api"

    @field_validator("title", "company", "source_job_id", "source_board_id")
    @classmethod
    def non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class RemotePolicy(BaseModel):
    allowed: set[RemoteStatus] = Field(default_factory=set)
    exclude: set[RemoteStatus] = Field(default_factory=set)
    reject_unknown: bool = False


class RoleTargets(BaseModel):
    include: list[str]
    exclude: list[str] = Field(default_factory=list)

    @field_validator("include")
    @classmethod
    def include_required(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("at least one target role is required")
        return value


class Skills(BaseModel):
    required: list[str] = Field(default_factory=list)
    preferred: list[str] = Field(default_factory=list)


class CandidateProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str
    target_roles: RoleTargets
    countries: set[str] = Field(default_factory=set)
    remote_policy: RemotePolicy = Field(default_factory=RemotePolicy)
    skills: Skills = Field(default_factory=Skills)
    employment_types: set[EmploymentType] = Field(default_factory=set)
    excluded_seniority: set[Seniority] = Field(default_factory=set)
    excluded_keywords: list[str] = Field(default_factory=list)
    notes: str | None = None


class JobMatch(BaseModel):
    job_id: str
    client_id: str
    decision: MatchDecision
    score: int | None = None
    matched_reasons: list[str] = Field(default_factory=list)
    rejection_reasons: list[str] = Field(default_factory=list)
    evaluated_at: datetime = Field(default_factory=utc_now)
    matcher_version: str


class SourceTarget(BaseModel):
    board_id: str
    company: str


class CollectionResult(BaseModel):
    source: str
    target: SourceTarget
    status: CollectionStatus
    jobs: list[Job] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def failures_are_not_empty_successes(self) -> CollectionResult:
        if self.status is not CollectionStatus.SUCCESS and not self.errors:
            raise ValueError("non-success collection requires an error")
        return self


class SourceRegistryEntry(BaseModel):
    name: str
    enabled: bool
    source_type: str
    access_mode: str
    official_api: bool
    base_url: HttpUrl
    authentication_required: bool
    credential_environment_variables: list[str] = Field(default_factory=list)
    attribution_required: bool | None = None
    commercial_use_notes: str | None = None
    rate_limit_notes: str | None = None
    supports_direct_apply_url: bool
    supports_description: bool
    supports_location: bool
    supports_remote_flag: bool
    supports_posted_at: bool
    supports_updated_at: bool
    last_verified_at: datetime
    documentation_reference: HttpUrl
