"""Recoverable Google Sheets publication for an explicitly configured batch destination."""

from __future__ import annotations

from datetime import UTC
from typing import Protocol
from urllib.parse import quote, unquote, urlparse

from job_scout.delivery_destinations import ClientSheetDestination
from job_scout.domain.daily_batch import BatchConflict, DailyBatchResult
from job_scout.export.csv_exporter import CSV_COLUMNS
from job_scout.storage.daily_batches import digest

SHEET_COLUMNS = [*CSV_COLUMNS, "Status", "Batch ID", "Batch Prepared At", "Job ID"]


def sheet_destination(spreadsheet_id: str, tab: str) -> str:
    if not spreadsheet_id or not all(c.isalnum() or c in "-_" for c in spreadsheet_id):
        raise BatchConflict("invalid spreadsheet ID")
    if not tab or any(c in tab for c in "\r\n!"):
        raise BatchConflict("invalid sheet tab")
    return f"gsheet://{spreadsheet_id}/{quote(tab, safe='')}"


def parse_sheet_destination(value: str) -> tuple[str, str]:
    parsed = urlparse(value)
    if (
        parsed.scheme != "gsheet"
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise BatchConflict("invalid Google Sheets destination")
    tab = unquote(parsed.path[1:])
    if value != sheet_destination(parsed.netloc, tab):
        raise BatchConflict("Google Sheets destination is not canonical")
    return parsed.netloc, tab


class SheetsGateway(Protocol):
    def read_rows(self, spreadsheet_id: str, tab: str) -> list[list[str]]: ...

    def append_rows(self, spreadsheet_id: str, tab: str, rows: list[list[str]]) -> None: ...

    def read_table_rows(self, spreadsheet_id: str, tab: str) -> list[list[str]]: ...

    def append_table_rows(
        self, spreadsheet_id: str, tab: str, rows: list[list[str]]
    ) -> None: ...

    def sheet_metadata(self, spreadsheet_id: str) -> list[dict[str, object]]: ...


class GoogleSheetsGateway:
    """Application Default Credentials; share the sheet with the runtime identity."""

    def __init__(self):
        try:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession
        except ImportError as error:
            raise OSError("install job-scout[sheets] for Google Sheets delivery") from error
        try:
            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
            self.session = AuthorizedSession(credentials)
        except Exception as error:
            raise OSError("Google Sheets runtime credentials are unavailable") from error

    @staticmethod
    def _range_url(spreadsheet_id: str, a1: str) -> str:
        return (
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
            f"/values/{quote(a1, safe='')}"
        )

    @classmethod
    def _url(cls, spreadsheet_id: str, tab: str) -> str:
        # Legacy fixed contract.
        a1 = "'" + tab.replace("'", "''") + "'!A:I"
        return cls._range_url(spreadsheet_id, a1)

    @classmethod
    def _table_url(cls, spreadsheet_id: str, tab: str) -> str:
        # A sheet-only A1 range lets Google return/append the used table width.
        a1 = "'" + tab.replace("'", "''") + "'"
        return cls._range_url(spreadsheet_id, a1)

    def read_rows(self, spreadsheet_id: str, tab: str) -> list[list[str]]:
        try:
            response = self.session.get(self._url(spreadsheet_id, tab), timeout=30)
            response.raise_for_status()
            return response.json().get("values", [])
        except Exception as error:
            raise OSError("Google Sheets read failed") from error

    def append_rows(self, spreadsheet_id: str, tab: str, rows: list[list[str]]) -> None:
        if not rows:
            return
        try:
            response = self.session.post(
                self._url(spreadsheet_id, tab) + ":append",
                params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                json={"majorDimension": "ROWS", "values": rows},
                timeout=60,
            )
            response.raise_for_status()
        except Exception as error:
            raise OSError("Google Sheets append outcome uncertain; retry this batch") from error

    def read_table_rows(self, spreadsheet_id: str, tab: str) -> list[list[str]]:
        try:
            response = self.session.get(self._table_url(spreadsheet_id, tab), timeout=30)
            response.raise_for_status()
            return response.json().get("values", [])
        except Exception as error:
            raise OSError("Google Sheets read failed") from error

    def append_table_rows(
        self, spreadsheet_id: str, tab: str, rows: list[list[str]]
    ) -> None:
        if not rows:
            return
        try:
            response = self.session.post(
                self._table_url(spreadsheet_id, tab) + ":append",
                params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                json={"majorDimension": "ROWS", "values": rows},
                timeout=60,
            )
            response.raise_for_status()
        except Exception as error:
            raise OSError("Google Sheets append outcome uncertain; retry this batch") from error

    def sheet_metadata(self, spreadsheet_id: str) -> list[dict[str, object]]:
        try:
            response = self.session.get(
                f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}",
                params={"fields": "sheets(properties(sheetId,title))"},
                timeout=30,
            )
            response.raise_for_status()
            return [
                {
                    "sheet_id": item.get("properties", {}).get("sheetId"),
                    "title": item.get("properties", {}).get("title"),
                }
                for item in response.json().get("sheets", [])
            ]
        except Exception as error:
            raise OSError("Google Sheets metadata read failed") from error


class BatchSheetPublisher:
    def __init__(self, result: DailyBatchResult, gateway: SheetsGateway):
        self.result = result
        self.gateway = gateway
        self.spreadsheet_id, self.tab = parse_sheet_destination(result.request.destination)

    def _read(self) -> list[list[str]]:
        values = self.gateway.read_rows(self.spreadsheet_id, self.tab)
        if not values or values[0] != SHEET_COLUMNS:
            raise BatchConflict("Google Sheets header differs from the delivery contract")
        if any(len(row) > len(SHEET_COLUMNS) for row in values[1:]):
            raise BatchConflict("Google Sheets rows exceed the delivery contract")
        if any(not any(row) for row in values[1:]):
            raise BatchConflict("Google Sheets contains an internal blank row; remove it before release")
        return [row + [""] * (len(SHEET_COLUMNS) - len(row)) for row in values]

    @staticmethod
    def _digest(values: list[list[str]]) -> str:
        # Operator Status can change without invalidating publication recovery.
        return digest([[cell for i, cell in enumerate(row) if i != 5] for row in values])

    def _rows(self, frozen: list[dict[str, str]]) -> list[list[str]]:
        if len(frozen) != len(self.result.items):
            raise BatchConflict("frozen rows do not match prepared batch items")
        prepared = self.result.assembled_at.astimezone(UTC).isoformat()
        return [
            [
                *(row[column] for column in CSV_COLUMNS),
                "",
                self.result.batch_id,
                prepared,
                item.representative_job_id,
            ]
            for row, item in zip(frozen, self.result.items)
        ]

    def plan(self, frozen: list[dict[str, str]]) -> tuple[str, str]:
        values = self._read()
        if any(row[6] == self.result.batch_id for row in values[1:]):
            raise BatchConflict("batch marker already exists without a delivery journal")
        additions = self._rows(frozen)
        return self._digest(values), self._digest(values + additions)

    def inspect(self) -> str:
        return self._digest(self._read())

    def publish(self, frozen: list[dict[str, str]], before: str, after: str) -> None:
        current = self.inspect()
        if current == after:
            return
        if current != before:
            raise BatchConflict("Google Sheets changed; reconcile before retry")
        self.gateway.append_rows(self.spreadsheet_id, self.tab, self._rows(frozen))
        if self.inspect() != after:
            raise BatchConflict("Google Sheets append could not be verified; reconcile")

class ClientSheetPublisher:
    """Publish a frozen batch into a client-owned, explicitly mapped Sheet."""

    def __init__(
        self,
        result: DailyBatchResult,
        gateway: SheetsGateway,
        destination: ClientSheetDestination,
    ):
        self.result = result
        self.gateway = gateway
        self.destination = destination
        if result.request.client_id != destination.client_id:
            raise BatchConflict("batch client does not own this delivery destination")
        if result.request.destination_id != destination.destination_id:
            raise BatchConflict("batch destination identity does not match registration")
        if result.request.destination_config_sha256 != destination.config_sha256:
            raise BatchConflict("client delivery destination changed after batch preparation")
        self.header = list(destination.header)
        self.target_index = {name: self.header.index(name) for name in destination.column_mapping.values()}
        self.owned = {
            self.header.index(target)
            for source, target in destination.column_mapping.items()
            if source != "Status"
        }

    def _read(self) -> list[list[str]]:
        metadata = self.gateway.sheet_metadata(self.destination.spreadsheet_id)
        current = [
            item
            for item in metadata
            if item.get("sheet_id") == self.destination.sheet_id
        ]
        if len(current) != 1:
            raise BatchConflict("registered Google Sheet tab no longer exists")
        if current[0].get("title") != self.destination.tab_name:
            raise BatchConflict(
                "Google Sheet tab was renamed; refresh the destination registration"
            )

        values = self.gateway.read_table_rows(
            self.destination.spreadsheet_id, self.destination.tab_name
        )
        if not values or tuple(values[0]) != self.destination.header:
            raise BatchConflict(
                "Google Sheets header differs from the registered client schema"
            )
        width = len(self.header)
        if any(len(row) > width for row in values[1:]):
            raise BatchConflict(
                "Google Sheets rows exceed the registered header width"
            )
        if any(not any(row) for row in values[1:-1]):
            raise BatchConflict(
                "Google Sheets contains an internal blank row; reconcile before release"
            )
        return [row + [""] * (width - len(row)) for row in values]

    def _digest(self, values: list[list[str]]) -> str:
        # Unmapped client-owned cells and mapped Status are intentionally mutable.
        body = [
            [row[index] for index in sorted(self.owned)]
            for row in values[1:]
        ]
        return digest(
            {
                "header_sha256": self.destination.header_sha256,
                "row_count": max(0, len(values) - 1),
                "owned_values": body,
            }
        )

    def _rows(self, frozen: list[dict[str, str]]) -> list[list[str]]:
        if len(frozen) != len(self.result.items):
            raise BatchConflict("frozen rows do not match prepared batch items")
        prepared = self.result.assembled_at.astimezone(UTC).isoformat()
        rows = []
        for row, item in zip(frozen, self.result.items):
            source_values = {
                **row,
                "Status": "",
                "Batch ID": self.result.batch_id,
                "Batch Prepared At": prepared,
                "Job ID": item.representative_job_id,
            }
            output = [""] * len(self.header)
            for source, target in self.destination.column_mapping.items():
                output[self.header.index(target)] = source_values[source]
            rows.append(output)
        return rows

    def plan(self, frozen: list[dict[str, str]]) -> tuple[str, str]:
        values = self._read()
        batch_target = self.destination.column_mapping.get("Batch ID")
        if batch_target is not None:
            index = self.header.index(batch_target)
            if any(row[index] == self.result.batch_id for row in values[1:]):
                raise BatchConflict(
                    "batch marker already exists without a delivery journal"
                )
        additions = self._rows(frozen)
        return self._digest(values), self._digest(values + additions)

    def inspect(self) -> str:
        return self._digest(self._read())

    def publish(self, frozen: list[dict[str, str]], before: str, after: str) -> None:
        current = self.inspect()
        if current == after:
            return
        if current != before:
            raise BatchConflict("Google Sheets changed; reconcile before retry")
        self.gateway.append_table_rows(
            self.destination.spreadsheet_id,
            self.destination.tab_name,
            self._rows(frozen),
        )
        if self.inspect() != after:
            raise BatchConflict("Google Sheets append could not be verified; reconcile")

