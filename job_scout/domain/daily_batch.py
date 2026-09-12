"""Daily Batch V1: explicit evidence inputs and unit-bearing immutable results."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BatchConflict(ValueError):
    """A key, evidence scope, or destination cannot safely be reused."""


class BatchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DailyBatchRequest(BatchModel):
    client_id: str
    destination: str
    idempotency_key: str
    requested_quota: int = Field(strict=True, ge=1)
    evidence_scope_id: str
    evaluation_id: str
    candidate_job_ids: tuple[str, ...]
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Legacy job_matches have no immutable revision association. Null is honest.
    brief_revision_id: str | None = None
    brief_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    completeness: Literal["complete", "partial", "unknown"] = "unknown"
    source_failures: tuple[str, ...] = ()

    @field_validator(
        "client_id", "destination", "idempotency_key", "evidence_scope_id", "evaluation_id"
    )
    @classmethod
    def non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("explicit nonblank identity required")
        return value.strip()

    @field_validator("candidate_job_ids")
    @classmethod
    def canonical_candidates(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values) or len(set(values)) != len(values):
            raise ValueError("candidate IDs must be nonblank and unique")
        return tuple(sorted(values))

    @model_validator(mode="after")
    def revision_attribution(self) -> DailyBatchRequest:
        if (self.brief_revision_id is None) != (self.brief_sha256 is None):
            raise ValueError("revision attribution requires both revision ID and artifact hash")
        if self.brief_revision_id is not None and not self.brief_revision_id.strip():
            raise ValueError("revision ID must not be blank")
        return self


class DailyBatchCounts(BatchModel):
    candidate_postings: int = Field(ge=0)
    match_eligible_postings: int = Field(ge=0)
    needs_review_postings: int = Field(ge=0)
    rejected_postings: int = Field(ge=0)
    match_eligible_groups: int = Field(ge=0)
    historically_suppressed_groups: int = Field(ge=0)
    previously_delivered_groups: int = Field(ge=0)
    duplicate_postings_collapsed: int = Field(ge=0)
    fresh_eligible_groups: int = Field(ge=0)
    selected_groups: int = Field(ge=0)

    @model_validator(mode="after")
    def accounting(self) -> DailyBatchCounts:
        if self.candidate_postings != (
            self.match_eligible_postings + self.needs_review_postings + self.rejected_postings
        ):
            raise ValueError("posting decisions must partition candidate postings")
        if self.match_eligible_postings != (
            self.match_eligible_groups + self.duplicate_postings_collapsed
        ):
            raise ValueError("duplicate accounting must retain posting/group units")
        if (
            self.match_eligible_groups
            != (
                self.historically_suppressed_groups
                + self.previously_delivered_groups
                + self.fresh_eligible_groups
            )
            or self.selected_groups > self.fresh_eligible_groups
        ):
            raise ValueError("group suppression/selection accounting does not balance")
        return self


class DailyBatchItem(BatchModel):
    ordinal: int = Field(ge=1)
    delivery_group_id: str
    representative_job_id: str
    evidence_sha256: str
    matcher_version: str


class DailyBatchResult(BatchModel):
    batch_id: str
    request: DailyBatchRequest
    status: Literal["prepared", "failed", "delivered"]
    assembled_at: datetime
    delivered_at: datetime | None = None
    counts: DailyBatchCounts
    selected_count: int = Field(ge=0)
    shortfall: int = Field(ge=0)
    items: tuple[DailyBatchItem, ...]
    dedupe_version: str
    error: str | None = None

    @model_validator(mode="after")
    def quota_invariants(self) -> DailyBatchResult:
        if not (
            self.selected_count == self.counts.selected_groups == len(self.items)
            and self.selected_count <= self.request.requested_quota
            and self.shortfall == self.request.requested_quota - self.selected_count
            and [i.ordinal for i in self.items] == list(range(1, self.selected_count + 1))
            and len({i.delivery_group_id for i in self.items}) == self.selected_count
        ):
            raise ValueError("invalid batch quota, shortfall, ordering or group uniqueness")
        return self
