from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CANONICAL_SHEET_FIELDS = (
    "Job Title",
    "Company Name",
    "Job Link",
    "Job Description",
    "Job Platform",
    "Status",
    "Batch ID",
    "Batch Prepared At",
    "Job ID",
)
REQUIRED_SHEET_FIELDS = {"Job Link"}


class DeliveryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorksheetMetadata(DeliveryModel):
    worksheet_id: int = Field(ge=0)
    title: str = Field(min_length=1)
    index: int = Field(ge=0)


class SheetDeliveryContract(DeliveryModel):
    """Immutable Google Sheets contract frozen into a prepared batch."""

    destination_id: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    spreadsheet_id: str = Field(min_length=1)
    worksheet_id: int = Field(ge=0)
    worksheet_name: str = Field(min_length=1)
    header_row: int = Field(default=1, ge=1)
    headers: tuple[str, ...] = Field(min_length=1)
    column_map: dict[str, int]
    header_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validated_at: datetime

    @field_validator("client_id", "worksheet_name")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("delivery identity must not be blank")
        return value

    @field_validator("destination_id", "campaign_id")
    @classmethod
    def stable_id(cls, value: str) -> str:
        value = value.strip()
        if not value or not all(
            character.isalnum() or character in "._-" for character in value
        ):
            raise ValueError(
                "destination and campaign IDs may contain only letters, numbers, dots, "
                "underscores, or hyphens"
            )
        return value

    @field_validator("spreadsheet_id")
    @classmethod
    def valid_spreadsheet_id(cls, value: str) -> str:
        value = value.strip()
        if not value or not all(
            character.isalnum() or character in "-_" for character in value
        ):
            raise ValueError("invalid spreadsheet ID")
        return value

    @model_validator(mode="after")
    def mapping_is_safe(self) -> SheetDeliveryContract:
        keys = set(self.column_map)
        unsupported = keys - set(CANONICAL_SHEET_FIELDS)
        if unsupported:
            raise ValueError(f"unsupported sheet fields: {', '.join(sorted(unsupported))}")
        if not REQUIRED_SHEET_FIELDS <= keys:
            raise ValueError("sheet mapping must include Job Link")
        indices = list(self.column_map.values())
        if len(indices) != len(set(indices)):
            raise ValueError("multiple JobSift fields cannot target the same sheet column")
        if any(index < 0 or index >= len(self.headers) for index in indices):
            raise ValueError("sheet mapping points outside the validated header row")
        if self.validated_at.tzinfo is None:
            raise ValueError("validated_at must include a timezone")
        return self


class DeliveryDestination(DeliveryModel):
    destination_id: str
    client_id: str
    spreadsheet_id: str
    worksheet_id: int
    worksheet_name: str
    header_row: int
    headers: tuple[str, ...]
    column_map: dict[str, int]
    header_sha256: str
    status: Literal["ready", "blocked"]
    validated_at: datetime
    created_at: datetime
    updated_at: datetime


class DeliveryCampaign(DeliveryModel):
    campaign_id: str
    client_id: str
    destination_id: str
    status: Literal["active", "paused", "completed"]
    created_at: datetime
    completed_at: datetime | None = None
