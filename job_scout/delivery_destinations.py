from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

from job_scout.domain.daily_batch import BatchConflict
from job_scout.domain.delivery import CANONICAL_SHEET_FIELDS, WorksheetMetadata
from job_scout.storage.destinations import DeliveryDestinationStore

_SPREADSHEET_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_ALIASES = {
    "Job Title": {"job title", "title", "role", "position", "job role"},
    "Company Name": {"company name", "company", "employer", "organization", "organisation"},
    "Job Link": {
        "job link",
        "link",
        "url",
        "job url",
        "application link",
        "apply link",
        "posting link",
    },
    "Job Description": {"job description", "description", "details"},
    "Job Platform": {"job platform", "platform", "source", "job source"},
    "Status": {"status", "application status"},
    "Batch ID": {"batch id"},
    "Batch Prepared At": {
        "batch prepared at",
        "date found",
        "found date",
        "date added",
        "sourced at",
    },
    "Job ID": {"job id", "posting id"},
}


def header_fingerprint(headers: tuple[str, ...]) -> str:
    payload = json.dumps(list(headers), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def normalize_header_row(values: list[str]) -> tuple[str, ...]:
    headers = [str(value).strip() for value in values]
    while headers and headers[-1] == "":
        headers.pop()
    if not headers:
        raise BatchConflict("client sheet has no header row")
    return tuple(headers)


def parse_google_sheet_url(value: str) -> tuple[str, int | None]:
    raw = value.strip()
    if _SPREADSHEET_ID.fullmatch(raw) and "/" not in raw:
        return raw, None
    parsed = urlparse(raw)
    if parsed.scheme != "https" or parsed.hostname != "docs.google.com":
        raise BatchConflict("expected a Google Sheets URL")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 3 or parts[:2] != ["spreadsheets", "d"]:
        raise BatchConflict("invalid Google Sheets URL")
    spreadsheet_id = parts[2]
    if not _SPREADSHEET_ID.fullmatch(spreadsheet_id):
        raise BatchConflict("invalid spreadsheet ID")
    gid_values = parse_qs(parsed.fragment).get("gid", [])
    gid = None
    if gid_values:
        try:
            gid = int(gid_values[0])
        except ValueError as error:
            raise BatchConflict("invalid worksheet gid in Google Sheets URL") from error
        if gid < 0:
            raise BatchConflict("invalid worksheet gid in Google Sheets URL")
    return spreadsheet_id, gid


def _select_worksheet(
    worksheets: list[WorksheetMetadata],
    *,
    gid: int | None,
    worksheet_name: str | None,
) -> WorksheetMetadata:
    if worksheet_name is not None:
        matches = [sheet for sheet in worksheets if sheet.title == worksheet_name]
        if len(matches) != 1:
            raise BatchConflict("requested worksheet name was not found")
        selected = matches[0]
        if gid is not None and selected.worksheet_id != gid:
            raise BatchConflict("sheet URL gid and worksheet name refer to different tabs")
        return selected
    if gid is not None:
        matches = [sheet for sheet in worksheets if sheet.worksheet_id == gid]
        if len(matches) != 1:
            raise BatchConflict("worksheet from Google Sheets URL was not found")
        return matches[0]
    if len(worksheets) == 1:
        return worksheets[0]
    raise BatchConflict(
        "spreadsheet has multiple worksheets; use a URL with gid or specify the worksheet name"
    )


def _header_index(headers: tuple[str, ...], header: str) -> int:
    matches = [index for index, value in enumerate(headers) if value == header]
    if len(matches) != 1:
        raise BatchConflict(f"sheet header must exist exactly once: {header}")
    return matches[0]


def propose_column_map(
    headers: tuple[str, ...],
    *,
    overrides: dict[str, str] | None = None,
) -> dict[str, int]:
    normalized: dict[str, list[int]] = {}
    for index, header in enumerate(headers):
        normalized.setdefault(header.casefold().strip(), []).append(index)

    mapping: dict[str, int] = {}
    for field, aliases in _ALIASES.items():
        candidates = {
            index
            for alias in aliases
            for index in normalized.get(alias.casefold(), [])
        }
        if len(candidates) == 1:
            mapping[field] = next(iter(candidates))

    for field, header in (overrides or {}).items():
        if field not in CANONICAL_SHEET_FIELDS:
            raise BatchConflict(f"unsupported JobSift sheet field: {field}")
        mapping[field] = _header_index(headers, header)

    if "Job Link" not in mapping:
        raise BatchConflict(
            "could not map Job Link; provide an explicit mapping for the client's link column"
        )
    if len(set(mapping.values())) != len(mapping):
        raise BatchConflict("multiple JobSift fields map to the same client column")
    return dict(sorted(mapping.items()))


def inspect_client_sheet(
    *,
    gateway,
    sheet_url: str,
    worksheet_name: str | None = None,
    header_row: int = 1,
    overrides: dict[str, str] | None = None,
) -> dict[str, object]:
    if header_row != 1:
        raise BatchConflict("only header row 1 is supported in destination V1")
    spreadsheet_id, gid = parse_google_sheet_url(sheet_url)
    worksheets = gateway.worksheets(spreadsheet_id)
    if not worksheets:
        raise BatchConflict("spreadsheet contains no worksheets")
    worksheet = _select_worksheet(
        worksheets,
        gid=gid,
        worksheet_name=worksheet_name,
    )
    rows = gateway.read_table(spreadsheet_id, worksheet.title, 100)
    if not rows:
        raise BatchConflict("client worksheet is empty")
    headers = normalize_header_row(rows[0])
    mapping = propose_column_map(headers, overrides=overrides)
    return {
        "spreadsheet_id": spreadsheet_id,
        "worksheet_id": worksheet.worksheet_id,
        "worksheet_name": worksheet.title,
        "header_row": header_row,
        "headers": headers,
        "column_map": mapping,
        "header_sha256": header_fingerprint(headers),
        "validated_at": datetime.now(UTC),
    }


def register_client_sheet(
    *,
    repository,
    gateway,
    client_id: str,
    destination_id: str,
    campaign_id: str,
    sheet_url: str,
    worksheet_name: str | None = None,
    overrides: dict[str, str] | None = None,
):
    inspected = inspect_client_sheet(
        gateway=gateway,
        sheet_url=sheet_url,
        worksheet_name=worksheet_name,
        overrides=overrides,
    )
    store = DeliveryDestinationStore(repository)
    destination = store.register_destination(
        destination_id=destination_id,
        client_id=client_id,
        **inspected,
    )
    campaign = store.bind_campaign(
        campaign_id=campaign_id,
        client_id=client_id,
        destination_id=destination_id,
    )
    return destination, campaign
