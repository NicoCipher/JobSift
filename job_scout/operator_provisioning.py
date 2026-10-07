"""Process one frontend operator provisioning request inside trusted runtime."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from job_scout.delivery_destinations import (
    ClientSheetDestinationStore,
    spreadsheet_id_from_value,
)
from job_scout.delivery_profiles import (
    ClientDeliveryProfileStore,
    delivery_profile_control_id,
)
from job_scout.domain.daily_batch import BatchConflict
from job_scout.domain.models import SearchBrief
from job_scout.export.batch_sheets import GoogleSheetsGateway
from job_scout.operator_clients import (
    OperatorClientStore,
    OperatorProvisioningStore,
)
from job_scout.storage.factory import create_repository

_REQUIRED_MAPPING = ("Job Title", "Company Name", "Job Link")
_HEADER_ALIASES = {
    "Job Title": ("job title", "title", "role", "position"),
    "Company Name": ("company name", "company", "employer"),
    "Job Link": ("job link", "link", "url", "application link", "apply link"),
    "Job Platform": ("job platform", "platform", "source"),
    "Job Description": ("job description", "description", "summary"),
}


class CriteriaPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role_titles: list[str]
    country: str
    work_modes: list[str]
    exclusions: list[str] = Field(default_factory=list)
    preferred_terms: list[str] = Field(default_factory=list)
    freshness_hours: int = Field(default=24, ge=1, le=24)

    @field_validator("role_titles", "work_modes")
    @classmethod
    def nonempty_list(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        if not cleaned:
            raise ValueError("at least one value is required")
        return cleaned

    @field_validator("country")
    @classmethod
    def country_code(cls, value: str) -> str:
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", value):
            raise ValueError("country must be a two-letter code")
        return value

    @field_validator("work_modes")
    @classmethod
    def valid_modes(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip().casefold() for item in value if item.strip()]
        if any(item not in {"remote", "hybrid", "onsite"} for item in cleaned):
            raise ValueError("work mode must be remote, hybrid, or onsite")
        return cleaned


class SheetPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    tab: str
    column_mapping: dict[str, str] = Field(default_factory=dict)

    @field_validator("url", "tab")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Sheet URL and tab are required")
        return value


class CreateClientPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_name: str
    criteria: CriteriaPayload
    daily_limit: int = Field(ge=1, le=5000)
    delivery_mode: str
    sheet: SheetPayload

    @field_validator("client_name")
    @classmethod
    def client_name_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("client name is required")
        return value[:120]

    @field_validator("delivery_mode")
    @classmethod
    def valid_delivery_mode(cls, value: str) -> str:
        value = value.strip().casefold()
        if value not in {"review", "auto"}:
            raise ValueError("delivery mode must be review or auto")
        return value


class InspectSheetPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sheet_url: str
    tab: str

    @field_validator("sheet_url", "tab")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Sheet URL and tab are required")
        return value


def _decode_payload(raw: str) -> dict[str, object]:
    try:
        decoded = base64.urlsafe_b64decode(raw.encode()).decode()
        value = json.loads(decoded)
    except Exception as exc:
        raise ValueError("invalid provisioning payload") from exc
    if not isinstance(value, dict):
        raise ValueError("provisioning payload must be an object")
    return value


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return normalized[:48] or "client"


def _client_id(name: str, request_id: str) -> str:
    suffix = hashlib.sha256(f"{request_id}:{name}".encode()).hexdigest()[:10]
    return f"{_slug(name)}-{suffix}"


def _search_brief(client_id: str, criteria: CriteriaPayload) -> SearchBrief:
    return SearchBrief.model_validate(
        {
            "client_id": client_id,
            "target_roles": criteria.role_titles,
            "target_market": {
                "countries": [criteria.country],
                "intent": "must",
                "unknown_policy": "reject",
            },
            "work_mode": {
                "modes": criteria.work_modes,
                "intent": "must",
                "unknown_policy": "reject",
            },
            "avoid_terms": criteria.exclusions,
            "preferred_terms": criteria.preferred_terms,
            "posting_freshness": {
                "max_age_hours": criteria.freshness_hours,
                "unknown_policy": "reject",
            },
        }
    )


def _normalized_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def propose_mapping(header: list[str]) -> dict[str, str]:
    normalized = {_normalized_header(value): value for value in header if value.strip()}
    mapping: dict[str, str] = {}
    for field, aliases in _HEADER_ALIASES.items():
        for alias in aliases:
            target = normalized.get(alias)
            if target is not None:
                mapping[field] = target
                break
    return mapping


def inspect_sheet(
    *,
    sheet_url: str,
    tab: str,
    gateway,
) -> dict[str, object]:
    spreadsheet_id = spreadsheet_id_from_value(sheet_url)
    metadata = gateway.sheet_metadata(spreadsheet_id)
    tabs = [
        str(item.get("title", "")).strip()
        for item in metadata
        if str(item.get("title", "")).strip()
    ]
    if tab not in tabs:
        raise BatchConflict("Google Sheets tab was not found uniquely")
    rows = gateway.read_rows(spreadsheet_id, tab)
    if not rows:
        raise BatchConflict("Google Sheets tab has no header row")
    header = [str(value) for value in rows[0]]
    mapping = propose_mapping(header)
    missing = [field for field in _REQUIRED_MAPPING if field not in mapping]
    return {
        "tabs": tabs,
        "selected_tab": tab,
        "headers": header,
        "proposed_mapping": mapping,
        "missing_required_fields": missing,
    }


def process_request(
    repository,
    *,
    request_id: str,
    operation: str,
    payload: dict[str, object],
    gateway,
) -> dict[str, object]:
    requests = OperatorProvisioningStore(repository)
    existing = requests.get(request_id)
    if existing is None:
        requests.create(request_id=request_id, operation=operation, payload=payload)
    current = requests.claim(request_id)
    if current.state == "completed":
        return current.result or {}

    try:
        if operation == "inspect_sheet":
            value = InspectSheetPayload.model_validate(payload)
            result = inspect_sheet(
                sheet_url=value.sheet_url,
                tab=value.tab,
                gateway=gateway,
            )
        elif operation == "create_client":
            value = CreateClientPayload.model_validate(payload)
            client_id = _client_id(value.client_name, request_id)
            destination_id = "jobs"
            destination_name = f"{value.client_name} Jobs"
            sourcing_plan_id = f"operator-{client_id}"
            brief = _search_brief(client_id, value.criteria)

            inspection = inspect_sheet(
                sheet_url=value.sheet.url,
                tab=value.sheet.tab,
                gateway=gateway,
            )
            mapping = value.sheet.column_mapping or inspection["proposed_mapping"]
            if not isinstance(mapping, dict):
                raise ValueError("Sheet column mapping is invalid")
            missing = [field for field in _REQUIRED_MAPPING if field not in mapping]
            if missing:
                raise ValueError(
                    "Sheet mapping is missing required fields: " + ", ".join(missing)
                )

            destination = ClientSheetDestinationStore(repository).register_google_sheet(
                client_id=client_id,
                destination_id=destination_id,
                display_name=destination_name,
                spreadsheet=value.sheet.url,
                tab_name=value.sheet.tab,
                column_mapping={str(k): str(v) for k, v in mapping.items()},
                gateway=gateway,
            )
            managed = OperatorClientStore(repository).upsert(
                client_id=client_id,
                display_name=value.client_name,
                destination_id=destination_id,
                destination_name=destination_name,
                sourcing_plan_id=sourcing_plan_id,
                brief=brief,
            )
            profile = ClientDeliveryProfileStore(repository).upsert(
                client_id=client_id,
                destination_id=destination_id,
                sourcing_plan_id=sourcing_plan_id,
                daily_quota=value.daily_limit,
                status="active",
                delivery_mode=value.delivery_mode,
                timezone="Africa/Lagos",
            )
            result = {
                "client_name": managed.display_name,
                "destination_name": managed.destination_name,
                "profile_id": delivery_profile_control_id(
                    profile.client_id,
                    profile.destination_id,
                ),
                "delivery_mode": profile.delivery_mode,
                "daily_limit": profile.daily_quota,
                "sheet_status": destination.status,
                "sheet_url": (
                    f"https://docs.google.com/spreadsheets/d/"
                    f"{destination.spreadsheet_id}/edit#gid={destination.sheet_id}"
                ),
                "brief_revision": managed.brief_revision,
            }
        else:
            raise ValueError("provisioning operation is not implemented yet")
    except Exception as exc:
        requests.fail(
            request_id,
            code=type(exc).__name__.upper(),
            message=str(exc),
        )
        raise

    requests.complete(request_id, result)
    return result


def main() -> None:
    request_id = os.getenv("JOBSIFT_PROVISION_REQUEST_ID", "").strip().casefold()
    operation = os.getenv("JOBSIFT_PROVISION_OPERATION", "").strip()
    payload_raw = os.getenv("JOBSIFT_PROVISION_PAYLOAD_B64", "").strip()
    if not request_id or not operation or not payload_raw:
        raise SystemExit("provisioning request ID, operation and payload are required")
    parsed = UUID(request_id)
    if str(parsed) != request_id:
        raise SystemExit("provisioning request ID must be canonical")

    repository = create_repository(
        Path(os.getenv("JOBSIFT_DATABASE", "/tmp/jobsift-provision.sqlite3"))
    )
    try:
        result = process_request(
            repository,
            request_id=request_id,
            operation=operation,
            payload=_decode_payload(payload_raw),
            gateway=GoogleSheetsGateway(),
        )
    except (BatchConflict, ValueError) as exc:
        print(
            "JOBSIFT_PROVISION_ERROR="
            + json.dumps(
                {
                    "request_id": request_id,
                    "operation": operation,
                    "message": str(exc)[:500],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        raise SystemExit(1) from exc
    except Exception as exc:
        print(
            "JOBSIFT_PROVISION_ERROR="
            + json.dumps(
                {
                    "request_id": request_id,
                    "operation": operation,
                    "message": (
                        "JobSift could not verify the Google Sheet or save the client."
                    ),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        raise SystemExit(1) from exc

    print(
        "JOBSIFT_PROVISION_RESULT="
        + json.dumps(
            {
                "request_id": request_id,
                "operation": operation,
                "result": result,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
