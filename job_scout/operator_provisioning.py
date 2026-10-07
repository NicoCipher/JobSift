"""Process one frontend operator provisioning request inside trusted runtime."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from job_scout.delivery_destinations import (
    ClientSheetDestinationStore,
    spreadsheet_id_from_value,
)
from job_scout.delivery_profiles import (
    ClientDeliveryProfile,
    delivery_profile_control_id,
)
from job_scout.domain.daily_batch import BatchConflict
from job_scout.domain.models import SearchBrief
from job_scout.export.batch_sheets import GoogleSheetsGateway
from job_scout.operator_clients import (
    OperatorProvisioningStore,
    brief_sha256,
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


def _read_payload(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
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


def _activate_operator_client(
    repository,
    *,
    request_id: str,
    client_id: str,
    client_name: str,
    destination,
    sourcing_plan_id: str,
    brief: SearchBrief,
    daily_limit: int,
    delivery_mode: str,
) -> dict[str, object]:
    now = datetime.now(UTC)
    digest = brief_sha256(brief)
    with repository.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        request_row = connection.execute(
            "SELECT state FROM operator_provisioning_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if request_row is None or request_row["state"] != "running":
            raise ValueError("provisioning request is not running")

        destination_row = connection.execute(
            "SELECT status,spreadsheet_id,sheet_id FROM client_sheet_destinations "
            "WHERE client_id=? AND destination_id=?",
            (client_id, destination.destination_id),
        ).fetchone()
        if destination_row is None or destination_row["status"] != "ready":
            raise BatchConflict("verified client Sheet destination is not ready")

        existing_client = connection.execute(
            "SELECT created_at,brief_revision FROM operator_clients WHERE client_id=?",
            (client_id,),
        ).fetchone()
        client_created_at = (
            existing_client["created_at"]
            if existing_client is not None
            else now.isoformat()
        )
        brief_revision = (
            int(existing_client["brief_revision"]) + 1
            if existing_client is not None
            else 1
        )
        connection.execute(
            "INSERT INTO operator_clients "
            "(client_id,display_name,destination_id,destination_name,sourcing_plan_id,"
            "search_brief_json,brief_sha256,brief_revision,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(client_id) DO UPDATE SET "
            "display_name=excluded.display_name,"
            "destination_id=excluded.destination_id,"
            "destination_name=excluded.destination_name,"
            "sourcing_plan_id=excluded.sourcing_plan_id,"
            "search_brief_json=excluded.search_brief_json,"
            "brief_sha256=excluded.brief_sha256,"
            "brief_revision=excluded.brief_revision,"
            "updated_at=excluded.updated_at",
            (
                client_id,
                client_name,
                destination.destination_id,
                destination.display_name,
                sourcing_plan_id,
                brief.model_dump_json(),
                digest,
                brief_revision,
                client_created_at,
                now.isoformat(),
            ),
        )

        existing_profile = connection.execute(
            "SELECT created_at FROM client_delivery_profiles "
            "WHERE client_id=? AND destination_id=?",
            (client_id, destination.destination_id),
        ).fetchone()
        profile_created_at = (
            datetime.fromisoformat(existing_profile["created_at"])
            if existing_profile is not None
            else now
        )
        profile = ClientDeliveryProfile(
            client_id=client_id,
            destination_id=destination.destination_id,
            sourcing_plan_id=sourcing_plan_id,
            daily_quota=daily_limit,
            status="active",
            delivery_mode=delivery_mode,
            timezone="Africa/Lagos",
            created_at=profile_created_at,
            updated_at=now,
        )
        connection.execute(
            "INSERT INTO client_delivery_profiles "
            "(client_id,destination_id,sourcing_plan_id,daily_quota,status,"
            "delivery_mode,timezone,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(client_id,destination_id) DO UPDATE SET "
            "sourcing_plan_id=excluded.sourcing_plan_id,"
            "daily_quota=excluded.daily_quota,"
            "status=excluded.status,"
            "delivery_mode=excluded.delivery_mode,"
            "timezone=excluded.timezone,"
            "updated_at=excluded.updated_at",
            (
                profile.client_id,
                profile.destination_id,
                profile.sourcing_plan_id,
                profile.daily_quota,
                profile.status,
                profile.delivery_mode,
                profile.timezone,
                profile.created_at.isoformat(),
                profile.updated_at.isoformat(),
            ),
        )

        result = {
            "client_name": client_name,
            "destination_name": destination.display_name,
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
            "brief_revision": brief_revision,
        }
        completed = connection.execute(
            "UPDATE operator_provisioning_requests "
            "SET state='completed',result_json=?,error_code=NULL,error_message=NULL,"
            "updated_at=? WHERE request_id=? AND state='running'",
            (
                json.dumps(
                    result,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
                now.isoformat(),
                request_id,
            ),
        )
        if completed.rowcount != 1:
            raise ValueError("provisioning request state changed before activation")
    return result


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
            result = _activate_operator_client(
                repository,
                request_id=request_id,
                client_id=client_id,
                client_name=value.client_name,
                destination=destination,
                sourcing_plan_id=sourcing_plan_id,
                brief=brief,
                daily_limit=value.daily_limit,
                delivery_mode=value.delivery_mode,
            )
        else:
            raise ValueError("provisioning operation is not implemented yet")
    except Exception as exc:
        requests.fail(
            request_id,
            code=type(exc).__name__.upper(),
            message=str(exc),
        )
        raise

    if operation != "create_client":
        requests.complete(request_id, result)
    return result


def main() -> None:
    request_id = os.getenv("JOBSIFT_PROVISION_REQUEST_ID", "").strip().casefold()
    operation = os.getenv("JOBSIFT_PROVISION_OPERATION", "").strip()
    payload_file = os.getenv("JOBSIFT_PROVISION_PAYLOAD_FILE", "").strip()
    if not request_id or not operation or not payload_file:
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
            payload=_read_payload(Path(payload_file)),
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
