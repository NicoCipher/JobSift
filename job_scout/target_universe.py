"""Offline derivation of known source targets from historical job links."""

from __future__ import annotations

import csv
import json
import re
import uuid
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any, Literal
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field, model_validator

from job_scout.domain.models import LeverTargetConfig, WorkdayTargetConfig

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_BOARD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_WORKDAY_HOST = re.compile(r"^(?P<tenant>[a-z0-9][a-z0-9-]*)\.wd\d+\.myworkdayjobs\.com$")
_LOCALE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$")
_GREENHOUSE_HOSTS = {"job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"}
_LEVER_HOSTS = {"jobs.lever.co": "global", "jobs.eu.lever.co": "eu"}


class HistoricalLink(BaseModel):
    """One historical job-link row, preserving the source's original URL."""

    model_config = ConfigDict(extra="forbid")

    sheet: str
    row: int = Field(ge=1)
    title: str = ""
    company: str = ""
    url: str
    status: str = ""
    order: int = Field(ge=0)


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sheet: str
    row: int
    url: str


class TargetRecord(BaseModel):
    """Strict, source-neutral record for a target that historical evidence supports."""

    model_config = ConfigDict(extra="forbid")

    target_identity: str
    source: Literal["greenhouse", "ashby", "workday", "lever"]
    coordinates: dict[str, str]
    company_hint: str | None = None
    historical_occurrence_count: int = Field(ge=1)
    distinct_historical_url_count: int = Field(ge=1)
    first_historical_occurrence: EvidenceReference
    last_historical_occurrence: EvidenceReference
    derivation_status: Literal["supported_target"] = "supported_target"

    @model_validator(mode="after")
    def coordinates_match_source_identity(self) -> TargetRecord:
        if self.source in {"greenhouse", "ashby"}:
            if set(self.coordinates) != {"board"} or not _BOARD.fullmatch(
                self.coordinates.get("board", "")
            ):
                raise ValueError(f"{self.source} requires one valid board coordinate")
            expected = f"{self.source}:{self.coordinates['board']}"
        elif self.source == "lever":
            if set(self.coordinates) != {"instance", "site"}:
                raise ValueError("lever requires instance and site coordinates")
            config = LeverTargetConfig.model_validate(self.coordinates)
            expected = f"lever:{config.board_id}"
        else:
            if set(self.coordinates) != {"host", "tenant", "site"}:
                raise ValueError("workday requires host, tenant, and site coordinates")
            config = WorkdayTargetConfig.model_validate(self.coordinates)
            expected = f"workday:{config.board_id}"
        if self.target_identity != expected:
            raise ValueError("target identity does not match source coordinates")
        return self


class TargetUniverse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    universe_version: Literal["target-universe-v1"] = "target-universe-v1"
    input_provenance: dict[str, Any]
    generated_at: str
    total_historical_rows: int
    historical_status_counts: dict[str, int]
    classification_counts: dict[str, int]
    recognized_rows_by_source: dict[str, int]
    target_counts_by_source: dict[str, int]
    target_records: list[TargetRecord]
    unresolved: dict[str, dict[str, Any]]
    target_occurrence_distribution: dict[str, dict[str, int | float | None]]


@dataclass(frozen=True)
class _Derivation:
    classification: str
    source: str | None = None
    coordinates: dict[str, str] | None = None
    identity: str | None = None
    reason: str | None = None


def _is_coordinate(value: str) -> bool:
    return bool(value) and not any(character.isspace() for character in value)


def _segments(url: str) -> list[str]:
    return [unquote(segment) for segment in urlsplit(url).path.split("/") if segment]


def derive_link(link: HistoricalLink) -> _Derivation:
    """Classify one link without guessing unsupported provider coordinates."""
    parsed = urlsplit(link.url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or not host:
        return _Derivation("malformed_url", reason="URL lacks an HTTP(S) host")
    parts = _segments(link.url)

    if host in _GREENHOUSE_HOSTS:
        if len(parts) >= 3 and parts[1] == "jobs" and _BOARD.fullmatch(parts[0]):
            board = parts[0]
            return _Derivation(
                "supported_target",
                source="greenhouse",
                coordinates={"board": board},
                identity=f"greenhouse:{board}",
            )
        return _Derivation(
            "recognized_source_unresolved_target",
            source="greenhouse",
            reason="hosted Greenhouse URL does not expose a board/jobs posting path",
        )

    if host == "jobs.ashbyhq.com":
        if (
            len(parts) in {2, 3}
            and _BOARD.fullmatch(parts[0])
            and (len(parts) == 2 or parts[2] == "application")
        ):
            try:
                uuid.UUID(parts[1])
            except ValueError:
                pass
            else:
                board = parts[0]
                return _Derivation(
                    "supported_target",
                    source="ashby",
                    coordinates={"board": board},
                    identity=f"ashby:{board}",
                )
        return _Derivation(
            "recognized_source_unresolved_target",
            source="ashby",
            reason="hosted Ashby URL does not expose a board-scoped posting UUID",
        )

    if host in _LEVER_HOSTS:
        if (
            len(parts) in {2, 3}
            and _is_coordinate(parts[0])
            and (len(parts) == 2 or parts[2] == "apply")
        ):
            try:
                uuid.UUID(parts[1])
            except ValueError:
                pass
            else:
                instance = _LEVER_HOSTS[host]
                site = parts[0]
                return _Derivation(
                    "supported_target",
                    source="lever",
                    coordinates={"instance": instance, "site": site},
                    identity=f"lever:{LeverTargetConfig(instance=instance, site=site).board_id}",
                )
        return _Derivation(
            "recognized_source_unresolved_target",
            source="lever",
            reason="hosted Lever URL does not expose a site-scoped posting UUID",
        )

    workday = _WORKDAY_HOST.fullmatch(host)
    if workday:
        offset = 1 if parts and _LOCALE.fullmatch(parts[0].casefold()) else 0
        route = parts[offset + 1 : offset + 3]
        valid_route = bool(route and route[0] == "job") or route == ["jobs", "details"]
        if len(parts) >= offset + 4 and _is_coordinate(parts[offset]) and valid_route:
            config = WorkdayTargetConfig(host=host, tenant=workday["tenant"], site=parts[offset])
            return _Derivation(
                "supported_target",
                source="workday",
                coordinates={"host": config.host, "tenant": config.tenant, "site": config.site},
                identity=f"workday:{config.board_id}",
            )
        return _Derivation(
            "recognized_source_unresolved_target",
            source="workday",
            reason="Workday URL lacks a supported locale/site/job or locale/site/jobs/details path",
        )

    if host.endswith((".greenhouse.io", ".ashbyhq.com", ".lever.co", ".myworkdayjobs.com")):
        return _Derivation(
            "recognized_source_unresolved_target",
            reason="recognized provider host lacks a supported target structure",
        )
    return _Derivation("unsupported_source", reason="not one of the four current source contracts")


def _evidence(link: HistoricalLink) -> EvidenceReference:
    return EvidenceReference(
        sheet=link.sheet,
        row=link.row,
        url=link.url,
    )


def _company_hint(links: list[HistoricalLink]) -> str | None:
    values = [link.company.strip() for link in links if link.company.strip()]
    if not values:
        return None
    counts = Counter(values)
    return min(counts, key=lambda value: (-counts[value], value.casefold(), value))


def _distribution(values: list[int]) -> dict[str, int | float | None]:
    if not values:
        return {"min": None, "median": None, "max": None}
    return {"min": min(values), "median": median(values), "max": max(values)}


def build_target_universe(
    links: Iterable[HistoricalLink], *, input_provenance: dict[str, Any], generated_at: str
) -> TargetUniverse:
    """Deduplicate supported target identities while preserving bounded evidence."""
    grouped: dict[str, list[HistoricalLink]] = defaultdict(list)
    derivations: dict[str, _Derivation] = {}
    classifications: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    recognized: Counter[str] = Counter()
    unresolved: dict[str, list[tuple[HistoricalLink, _Derivation]]] = defaultdict(list)
    ordered = sorted(
        links, key=lambda link: (link.order, link.sheet.casefold(), link.row, link.url)
    )

    for link in ordered:
        statuses[link.status or "(blank)"] += 1
        derivation = derive_link(link)
        classifications[derivation.classification] += 1
        if derivation.source:
            recognized[derivation.source] += 1
        if derivation.classification == "supported_target":
            assert derivation.identity and derivation.coordinates and derivation.source
            grouped[derivation.identity].append(link)
            derivations[derivation.identity] = derivation
        else:
            unresolved[derivation.reason or derivation.classification].append((link, derivation))

    records: list[TargetRecord] = []
    source_counts: Counter[str] = Counter()
    by_source_occurrences: dict[str, list[int]] = defaultdict(list)
    for identity in sorted(
        grouped, key=lambda value: (derivations[value].source or "", value.casefold())
    ):
        values = grouped[identity]
        derivation = derivations[identity]
        assert derivation.source and derivation.coordinates
        source_counts[derivation.source] += 1
        by_source_occurrences[derivation.source].append(len(values))
        distinct = sorted({link.url for link in values})
        records.append(
            TargetRecord(
                target_identity=identity,
                source=derivation.source,
                coordinates=derivation.coordinates,
                company_hint=_company_hint(values),
                historical_occurrence_count=len(values),
                distinct_historical_url_count=len(distinct),
                first_historical_occurrence=_evidence(values[0]),
                last_historical_occurrence=_evidence(values[-1]),
            )
        )

    unresolved_payload: dict[str, dict[str, Any]] = {}
    for reason, values in sorted(unresolved.items()):
        classes = Counter(item.classification for _, item in values)
        sources = Counter(item.source for _, item in values if item.source)
        unresolved_payload[reason] = {
            "count": len(values),
            "classifications": dict(sorted(classes.items())),
            "sources": dict(sorted(sources.items())),
            "examples": [_evidence(link).model_dump(mode="json") for link, _ in values[:10]],
        }

    return TargetUniverse(
        input_provenance=input_provenance,
        generated_at=generated_at,
        total_historical_rows=len(ordered),
        historical_status_counts=dict(sorted(statuses.items())),
        classification_counts=dict(sorted(classifications.items())),
        recognized_rows_by_source=dict(sorted(recognized.items())),
        target_counts_by_source=dict(sorted(source_counts.items())),
        target_records=records,
        unresolved=unresolved_payload,
        target_occurrence_distribution={
            source: _distribution(values)
            for source, values in sorted(by_source_occurrences.items())
        },
    )


def _column_index(reference: str) -> int:
    letters = "".join(character for character in reference if character.isalpha())
    result = 0
    for character in letters:
        result = result * 26 + ord(character.upper()) - ord("A") + 1
    return result - 1


def _text(element: ElementTree.Element) -> str:
    return "".join(node.text or "" for node in element.iter(f"{_NS}t"))


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [_text(item) for item in root.findall(f"{_NS}si")]


def _xlsx_sheets(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    relations = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        item.attrib["Id"]: item.attrib["Target"].lstrip("/")
        for item in relations.findall(f"{_PKG_REL_NS}Relationship")
    }
    sheets: list[tuple[str, str]] = []
    for item in workbook.findall(f"{_NS}sheets/{_NS}sheet"):
        relation = item.attrib[f"{_REL_NS}id"]
        target = targets[relation]
        path = target if target.startswith("xl/") else f"xl/{target}"
        sheets.append((item.attrib["name"], path))
    return sheets


def _xlsx_rows(
    archive: zipfile.ZipFile, path: str, shared: list[str]
) -> list[tuple[int, dict[int, str]]]:
    root = ElementTree.fromstring(archive.read(path))
    rows: list[tuple[int, dict[int, str]]] = []
    for row in root.findall(f".//{_NS}sheetData/{_NS}row"):
        values: dict[int, str] = {}
        for cell in row.findall(f"{_NS}c"):
            reference = cell.attrib.get("r", "")
            index = _column_index(reference)
            kind = cell.attrib.get("t")
            if kind == "inlineStr":
                value = _text(cell)
            else:
                raw = cell.findtext(f"{_NS}v") or ""
                value = shared[int(raw)] if kind == "s" and raw else raw
            values[index] = value.strip()
        rows.append((int(row.attrib["r"]), values))
    return rows


def read_historical_xlsx(path: Path) -> list[HistoricalLink]:
    """Read rows from the original operator workbook without an XLSX dependency."""
    links: list[HistoricalLink] = []
    with zipfile.ZipFile(path) as archive:
        shared = _xlsx_shared_strings(archive)
        for sheet, worksheet_path in _xlsx_sheets(archive):
            rows = _xlsx_rows(archive, worksheet_path, shared)
            header_row = next(
                (
                    (number, values)
                    for number, values in rows[:10]
                    if any(value.casefold() == "links" for value in values.values())
                ),
                None,
            )
            if header_row is None:
                continue
            number, headers = header_row
            fields = {value.casefold(): index for index, value in headers.items()}
            url_index = fields["links"]
            for row_number, values in rows:
                if row_number <= number:
                    continue
                url = values.get(url_index, "").strip()
                if not url:
                    continue
                links.append(
                    HistoricalLink(
                        sheet=sheet,
                        row=row_number,
                        title=values.get(fields.get("title", -1), ""),
                        company=values.get(fields.get("company name", -1), ""),
                        url=url,
                        status=values.get(fields.get("status", -1), ""),
                        order=len(links),
                    )
                )
    return links


def read_historical_csv(path: Path) -> list[HistoricalLink]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        return [
            HistoricalLink(
                sheet=row.get("sheet", "csv"),
                row=int(row.get("row") or index),
                title=row.get("title", ""),
                company=row.get("company", ""),
                url=row.get("url", ""),
                status=row.get("status", ""),
                order=index,
            )
            for index, row in enumerate(reader, start=2)
            if (row.get("url") or "").strip()
        ]


def read_historical_links(path: Path) -> list[HistoricalLink]:
    if path.suffix.casefold() == ".xlsx":
        return read_historical_xlsx(path)
    if path.suffix.casefold() == ".csv":
        return read_historical_csv(path)
    raise ValueError("historical input must be .xlsx or .csv")


def write_universe(path: Path, universe: TargetUniverse) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(universe.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat()
