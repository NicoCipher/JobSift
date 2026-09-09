"""Operator historical-link evidence and conservative source identity parsing."""

from __future__ import annotations

import hashlib
import re
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from job_scout.normalization.core import canonicalize_url
from job_scout.target_universe import (
    _xlsx_rows,
    _xlsx_shared_strings,
    _xlsx_sheets,
    read_historical_links,
)

_BOARD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_WORKDAY_HOST = re.compile(r"^(?P<tenant>[a-z0-9][a-z0-9-]*)\.wd\d+\.myworkdayjobs\.com$")
_LOCALE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$")
_WORKDAY_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,99}$")


@dataclass(frozen=True)
class HistoricalRecord:
    original_url: str
    normalized_url: str
    source: str | None
    source_board_id: str | None
    source_job_id: str | None
    title: str
    company: str
    operator_status: str
    source_sheet: str
    source_row: int


@dataclass(frozen=True)
class HistoricalBlacklistEvidence:
    value: str
    kind: str
    source_sheet: str
    source_row: int


def _parts(url: str) -> list[str]:
    return [unquote(part) for part in urlsplit(url).path.split("/") if part]


def source_identity(url: str) -> tuple[str | None, str | None, str | None]:
    """Return a provider identity only when URL structure makes all parts explicit."""
    parsed = urlsplit(url)
    host, parts = (parsed.hostname or "").casefold(), _parts(url)
    if (
        host in {"job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"}
        and len(parts) >= 3
        and parts[1] == "jobs"
        and _BOARD.fullmatch(parts[0])
        and parts[2]
    ):
        return "greenhouse", parts[0], parts[2]
    if (
        host == "jobs.ashbyhq.com"
        and len(parts) in {2, 3}
        and _BOARD.fullmatch(parts[0])
        and (len(parts) == 2 or parts[2] == "application")
    ):
        try:
            uuid.UUID(parts[1])
        except ValueError:
            pass
        else:
            return "ashby", parts[0], parts[1]
    if (
        host in {"jobs.lever.co", "jobs.eu.lever.co"}
        and len(parts) in {2, 3}
        and parts[0]
        and (len(parts) == 2 or parts[2] == "apply")
    ):
        try:
            uuid.UUID(parts[1])
        except ValueError:
            pass
        else:
            return (
                "lever",
                f"{'global' if host == 'jobs.lever.co' else 'eu'}:{parts[0]}",
                parts[1],
            )
    workday = _WORKDAY_HOST.fullmatch(host)
    if workday:
        offset = 1 if parts and _LOCALE.fullmatch(parts[0].casefold()) else 0
        route = parts[offset + 1 : offset + 3]
        if len(parts) >= offset + 4 and parts[offset] and route and route[0] == "job":
            external_path = parts[-2] if parts[-1] == "apply" else parts[-1]
            _, separator, provider_id = external_path.rpartition("_")
            if not separator or not _WORKDAY_JOB_ID.fullmatch(provider_id):
                return None, None, None
            board = f"{host}:{workday['tenant']}:{parts[offset]}"
            return "workday", board, provider_id
    return None, None, None


def operator_status(value: str) -> str:
    return {"applied": "applied", "not applied": "not_applied"}.get(
        value.strip().casefold(), "unknown"
    )


def historical_records(path: str | Path) -> list[HistoricalRecord]:
    records = []
    for link in read_historical_links(Path(path)):
        source, board, job = source_identity(link.url)
        records.append(
            HistoricalRecord(
                original_url=link.url,
                normalized_url=canonicalize_url(link.url),
                source=source,
                source_board_id=board,
                source_job_id=job,
                title=link.title,
                company=link.company,
                operator_status=operator_status(link.status),
                source_sheet=link.sheet,
                source_row=link.row,
            )
        )
    return records


def explicit_blacklist_evidence(path: str | Path) -> list[HistoricalBlacklistEvidence]:
    """Read exact values explicitly placed on a workbook blacklist sheet."""
    workbook = Path(path)
    if workbook.suffix.casefold() != ".xlsx":
        return []
    entries: list[HistoricalBlacklistEvidence] = []
    with zipfile.ZipFile(workbook) as archive:
        shared = _xlsx_shared_strings(archive)
        for sheet, worksheet_path in _xlsx_sheets(archive):
            if "blacklist" not in sheet.casefold():
                continue
            for row, values in _xlsx_rows(archive, worksheet_path, shared):
                for value in values.values():
                    text = value.strip()
                    if not text:
                        continue
                    entries.append(
                        HistoricalBlacklistEvidence(
                            value=text,
                            kind="note" if "bid" in text.casefold() else "company",
                            source_sheet=sheet,
                            source_row=row,
                        )
                    )
    return entries


def workbook_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
