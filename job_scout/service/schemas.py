"""Executable V1 wire models, shared by runtime serialization and OpenAPI."""

from datetime import UTC, datetime
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from job_scout.domain.models import MatchDecision, RemoteStatus

T = TypeVar("T")
Availability = Literal["reported", "not_reported", "unknown"]
Completeness = Literal["complete", "partial", "unknown"]


class Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_serializer("*", check_fields=False)
    def utc_timestamp(self, value):
        if isinstance(value, datetime):
            return (
                value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value.tzinfo else None
            )
        return value


class Fact[T](Wire):
    value: T | None = None
    availability: Availability = "not_reported"

    @model_validator(mode="after")
    def coherent(self):
        if (self.availability == "reported") != (self.value is not None):
            raise ValueError("availability and value disagree")
        return self


class Metric(Fact[int]):
    value: int | None = Field(default=None, ge=0, strict=True)
    unit: Literal["postings", "groups", "history_entries", "clients", "briefs", "revisions"]
    definition: str


class Capability(Wire):
    allowed: bool = False
    reason: (
        Literal["not_implemented", "not_authorized", "evidence_unavailable", "state_conflict"]
        | None
    ) = "not_implemented"


class Scope(Wire):
    client_id: str | None = None
    brief_revision_id: str | None = None
    run_id: str | None = None
    destination_id: str | None = None
    cohort_id: str | None = None


class Failure(Wire):
    target_identity: str
    status: str
    evidence_ref: str | None = None


class Meta(Wire):
    request_id: str
    scope: Scope = Field(default_factory=Scope)
    observed_at: datetime | None = None
    served_at: datetime
    data_state: Literal["available", "stale", "unavailable"] = "available"
    completeness: Completeness = "unknown"
    source_failures: list[Failure] = Field(default_factory=list)
    snapshot_id: str | None = None
    limitations: list[str] = Field(default_factory=list)
    supported_filters: list[str] = Field(default_factory=list)
    supported_sorts: list[str] = Field(default_factory=list)


class PageInfo(Wire):
    limit: Literal[20, 40, 80]
    next_cursor: str | None
    previous_cursor: str | None
    known_total: Metric
    snapshot_id: str
    expires_at: datetime


class Envelope[T](Wire):
    data: T
    meta: Meta


class ListEnvelope[T](Envelope[list[T]]):
    page: PageInfo


class ClientScope(Wire):
    client_id: str
    capabilities: dict[str, Capability]


class Session(Wire):
    operator_id: str
    display_name: str
    authentication_mode: Literal["trusted_development"] = "trusted_development"
    expires_at: datetime | None = None
    csrf_token: str | None = None
    authorization_version: str
    capabilities: dict[str, Capability]
    client_scopes: list[ClientScope]


class Destination(Wire):
    destination_id: str
    display_name: str


class Client(Wire):
    client_id: str
    display_name: str
    detail_url: str
    briefs_url: str
    destinations: list[Destination] = Field(default_factory=list)
    capabilities: dict[str, Capability]


class ApplicationDestination(Wire):
    application_url: str | None = None
    canonical_url: str | None = None
    application_url_kind: Literal["direct_apply", "vacancy_page", "unavailable"] = "unavailable"


class MatchEvidence(Wire):
    decision: MatchDecision
    matched_reasons: list[str]
    rejection_reasons: list[str]
    matcher_version: str
    evaluated_at: datetime
    brief_revision_id: str | None = None
    run_id: str | None = None
    matched_role: Fact[str] = Field(default_factory=Fact[str])
    review_reasons: Fact[list[str]] = Field(default_factory=Fact[list[str]])
    preferred_term_hits: Fact[list[str]] = Field(default_factory=Fact[list[str]])


class DeliveryState(Wire):
    destination_id: str | None = None
    previously_delivered: Fact[bool] = Field(default_factory=Fact[bool])
    delivery_records: list[str] = Field(default_factory=list)
    historical_suppression: Fact[bool] = Field(default_factory=Fact[bool])
    historical_evidence_refs: list[str] = Field(default_factory=list)
    fresh_for_delivery: Fact[bool] = Field(default_factory=Fact[bool])
    non_delivery_reasons: Fact[list[str]] = Field(default_factory=Fact[list[str]])


class OutcomeSummary(Fact[Literal["applied", "not_applied", "unknown"]]):
    resolution: Literal["single", "conflicting", "not_recorded"] = "not_recorded"
    evidence_refs: list[str] = Field(default_factory=list)
    baseline_value: Literal["applied", "not_applied", "unknown"] | None = None
    current_source: Literal["unknown", "historical_import", "explicit_event"] | None = None
    version: int | None = None
    latest_event_id: str | None = None


class Provenance(Wire):
    evidence_ref: str | None = None
    brief_revision_id: str | None = None
    run_id: str | None = None
    source_target: dict[str, str] = Field(default_factory=dict)
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    raw_payload: Fact[str] = Field(default_factory=Fact[str])


class Posting(Wire):
    resource_type: Literal["posting"] = "posting"
    posting_id: str
    source: str
    source_board_id: str
    source_job_id: str
    title: str
    company: str
    location_text: str | None
    remote_status: RemoteStatus
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    application_destination: ApplicationDestination
    match: MatchEvidence | None
    provenance: Provenance = Field(default_factory=Provenance)
    description_text: str | None = None
    posted_at: datetime | None = None
    updated_at: datetime | None = None
    delivery_group_id: str | None = None
    delivery_state: DeliveryState
    outcome_summary: OutcomeSummary
    capabilities: dict[str, Capability]
    detail_url: str


class Group(Wire):
    resource_type: Literal["delivery_group"] = "delivery_group"
    delivery_group_id: str
    representative_posting: Posting | None
    representative_basis: Fact[str]
    member_count: Metric
    member_completeness: Completeness = "unknown"
    match: MatchEvidence | None
    delivery_state: DeliveryState
    outcome_summary: OutcomeSummary = Field(default_factory=OutcomeSummary)
    detail_url: str
    members_url: str
    members: list[Posting]
    provenance: Provenance = Field(default_factory=Provenance)
    grouping_evidence: Fact[str] = Field(default_factory=Fact[str])
    capabilities: dict[str, Capability]


class HistoryProvenance(Wire):
    import_id: str | None = None
    workbook_sha256: str | None = None
    sheet: str | None = None
    row: int | None = None


class HistoryEntry(Wire):
    history_entry_id: str
    client_id: str
    event_type: Literal["imported_history", "delivery_event"]
    operator_status: Literal["applied", "not_applied", "unknown"]
    original_url: str | None = None
    normalized_url: str | None = None
    source: str | None = None
    source_board_id: str | None = None
    source_job_id: str | None = None
    title: str | None = None
    company: str | None = None
    posting_id: str | None = None
    delivery_group_id: str | None = None
    destination_id: str | None = None
    imported_at: datetime | None = None
    exported_at: datetime | None = None
    outcome_at: datetime | None = None
    provenance: HistoryProvenance = Field(default_factory=HistoryProvenance)
    prior_surfacing: Fact[bool] = Field(
        default_factory=lambda: Fact(value=True, availability="reported")
    )
    outcome_summary: OutcomeSummary
    capabilities: dict[str, Capability]
    detail_url: str


class Brief(Wire):
    brief_id: str
    client_id: str
    revisions_url: str
    registered_at: datetime
    binding: Fact[str] = Field(default_factory=Fact[str])
    capabilities: dict[str, Capability]


class BriefProvenance(Wire):
    evidence_ref: str
    kind: Literal["registered_file"] = "registered_file"
    hash_basis: Literal["exact_artifact_bytes"] = "exact_artifact_bytes"


class BriefRevision(Wire):
    brief_revision_id: str
    brief_id: str
    client_id: str
    schema_version: Literal["operator-style-sourcing-brief-v1"]
    revision_label: str
    content_sha256: str
    rules: dict
    created_at: datetime | None = None
    registered_at: datetime
    provenance: BriefProvenance
    binding: Fact[str] = Field(default_factory=Fact[str])
    capabilities: dict[str, Capability]
