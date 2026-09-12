"""Operator outcomes are separate from matcher decisions and delivery facts."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Identity = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
Outcome = Literal["applied", "not_applied", "unknown"]
WritableOutcome = Literal["applied", "not_applied"]
SubjectType = Literal["posting", "history_entry"]


class OutcomeConflict(ValueError):
    """Stale version, incompatible replay, or unsafe subject resolution."""


class OutcomeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OperatorOutcomeSubject(OutcomeModel):
    type: SubjectType
    id: Identity

    @model_validator(mode="after")
    def history_identity(self):
        if self.type == "history_entry" and (
            not self.id.isascii()
            or not self.id.isdigit()
            or str(int(self.id)) != self.id
            or int(self.id) < 1
        ):
            raise ValueError("history subject must name a positive persisted row ID")
        return self


class OperatorOutcomeCommand(OutcomeModel):
    client_id: Identity
    subject: OperatorOutcomeSubject
    value: WritableOutcome
    expected_version: int = Field(strict=True, ge=0)
    actor_type: Identity
    actor_id: Identity
    idempotency_key: str = Field(min_length=16, max_length=128)
    source_reference: Identity


class OperatorOutcomeProjection(OutcomeModel):
    client_id: str
    subject: OperatorOutcomeSubject
    value: Outcome
    baseline_value: Outcome
    baseline_source: Literal["unknown", "historical_import"]
    baseline_import_id: str | None = None
    current_source: Literal["unknown", "historical_import", "explicit_event"]
    version: int = Field(ge=0)
    latest_event_id: str | None = None


class OperatorOutcomeEvent(OutcomeModel):
    event_id: str
    client_id: str
    subject: OperatorOutcomeSubject
    value: WritableOutcome
    version: int
    previous_value: Outcome
    previous_version: int
    actor_type: str
    actor_id: str
    recorded_at: datetime
    action: Literal["record_outcome", "import_outcome"]
    idempotency_key: str
    request_sha256: str
    source_reference: str


class OperatorOutcomeResult(OutcomeModel):
    event: OperatorOutcomeEvent | None
    projection: OperatorOutcomeProjection


class SurfacingEvidence(OutcomeModel):
    kind: Literal["export", "group_delivery", "historical_import"]
    reference: str
    recorded_at: datetime
    destination: str | None = None


class OperatorOutcomeImportRecord(OutcomeModel):
    external_record_id: Identity
    client_id: Identity
    subject_type: SubjectType
    source: Identity | None = None
    source_board_id: Identity | None = None
    source_job_id: Identity | None = None
    vacancy_url: str | None = None
    observed_outcome: Outcome
    source_reference: Identity
    expected_version: int = Field(strict=True, ge=0)
    actor_type: Identity
    actor_id: Identity

    @model_validator(mode="after")
    def evidence_identity(self):
        identity = (self.source, self.source_board_id, self.source_job_id)
        if any(v is not None for v in identity) and not all(v is not None for v in identity):
            raise ValueError("provider identity requires all three fields")
        if not all(identity) and not self.vacancy_url:
            raise ValueError("provider identity or vacancy URL required")
        return self


class OperatorOutcomeImportReceipt(OutcomeModel):
    import_record_id: str
    record: OperatorOutcomeImportRecord
    recorded_at: datetime
    result: OperatorOutcomeResult
