"""Recoverable Google Sheets publication for an explicitly configured batch destination."""

from __future__ import annotations

from datetime import UTC
from typing import Protocol
from urllib.parse import quote, unquote, urlparse

from job_scout.domain.daily_batch import BatchConflict, DailyBatchResult
from job_scout.domain.delivery import SheetDeliveryContract, WorksheetMetadata
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

    def worksheets(self, spreadsheet_id: str) -> list[WorksheetMetadata]: ...

    def read_table(self, spreadsheet_id: str, tab: str, width: int) -> list[list[str]]: ...

    def append_table_rows(
        self, spreadsheet_id: str, tab: str, width: int, rows: list[list[str]]
    ) -> None: ...


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
    def _column_name(width: int) -> str:
        if width < 1:
            raise ValueError("sheet width must be positive")
        value = width
        result = ""
        while value:
            value, remainder = divmod(value - 1, 26)
            result = chr(ord("A") + remainder) + result
        return result

    @classmethod
    def _url(cls, spreadsheet_id: str, tab: str, width: int = 9) -> str:
        # Quote a Sheet tab as an A1 sheet name, including embedded apostrophes.
        end = cls._column_name(width)
        a1 = "'" + tab.replace("'", "''") + f"'!A:{end}"
        return (
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
            f"/values/{quote(a1, safe='')}"
        )

    def read_rows(self, spreadsheet_id: str, tab: str) -> list[list[str]]:
        try:
            response = self.session.get(self._url(spreadsheet_id, tab, 9), timeout=30)
            response.raise_for_status()
            return response.json().get("values", [])
        except Exception as error:
            raise OSError("Google Sheets read failed") from error

    def append_rows(self, spreadsheet_id: str, tab: str, rows: list[list[str]]) -> None:
        if not rows:
            return
        try:
            response = self.session.post(
                self._url(spreadsheet_id, tab, 9) + ":append",
                params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                json={"majorDimension": "ROWS", "values": rows},
                timeout=60,
            )
            response.raise_for_status()
        except Exception as error:
            raise OSError("Google Sheets append outcome uncertain; retry this batch") from error


    def worksheets(self, spreadsheet_id: str) -> list[WorksheetMetadata]:
        try:
            response = self.session.get(
                f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}",
                params={"fields": "sheets(properties(sheetId,title,index))"},
                timeout=30,
            )
            response.raise_for_status()
            values = []
            for sheet in response.json().get("sheets", []):
                properties = sheet.get("properties") or {}
                values.append(
                    WorksheetMetadata(
                        worksheet_id=properties["sheetId"],
                        title=properties["title"],
                        index=properties["index"],
                    )
                )
            return sorted(values, key=lambda value: value.index)
        except Exception as error:
            raise OSError("Google Sheets metadata read failed") from error

    def read_table(self, spreadsheet_id: str, tab: str, width: int) -> list[list[str]]:
        try:
            response = self.session.get(self._url(spreadsheet_id, tab, width), timeout=30)
            response.raise_for_status()
            return response.json().get("values", [])
        except Exception as error:
            raise OSError("Google Sheets table read failed") from error

    def append_table_rows(
        self, spreadsheet_id: str, tab: str, width: int, rows: list[list[str]]
    ) -> None:
        if not rows:
            return
        try:
            response = self.session.post(
                self._url(spreadsheet_id, tab, width) + ":append",
                params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                json={"majorDimension": "ROWS", "values": rows},
                timeout=60,
            )
            response.raise_for_status()
        except Exception as error:
            raise OSError("Google Sheets append outcome uncertain; retry this batch") from error


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

class ManagedBatchSheetPublisher:
    """Publish a frozen batch through a client-owned, validated sheet contract."""

    def __init__(
        self,
        result: DailyBatchResult,
        gateway: SheetsGateway,
        contract: SheetDeliveryContract,
    ):
        self.result = result
        self.gateway = gateway
        self.contract = contract
        if result.request.client_id != contract.client_id:
            raise BatchConflict("batch client does not own the delivery contract")
        expected = sheet_destination(contract.spreadsheet_id, contract.worksheet_name)
        if result.request.destination != expected:
            raise BatchConflict("batch destination differs from the frozen delivery contract")

    def _worksheet(self) -> WorksheetMetadata:
        matches = [
            value
            for value in self.gateway.worksheets(self.contract.spreadsheet_id)
            if value.worksheet_id == self.contract.worksheet_id
        ]
        if len(matches) != 1:
            raise BatchConflict("client worksheet no longer exists")
        worksheet = matches[0]
        if worksheet.title != self.contract.worksheet_name:
            raise BatchConflict("client worksheet was renamed; revalidate the destination")
        return worksheet

    @staticmethod
    def _normalized_header(values: list[str]) -> tuple[str, ...]:
        result = [str(value) for value in values]
        while result and result[-1] == "":
            result.pop()
        return tuple(result)

    @staticmethod
    def _header_digest(headers: tuple[str, ...]) -> str:
        return digest(list(headers))

    def _read(self) -> list[list[str]]:
        self._worksheet()
        values = self.gateway.read_table(
            self.contract.spreadsheet_id,
            self.contract.worksheet_name,
            len(self.contract.headers),
        )
        if not values:
            raise BatchConflict("client sheet has no header row")
        headers = self._normalized_header(values[0])
        if headers != self.contract.headers:
            raise BatchConflict("client sheet headers changed; revalidate the destination")
        if self._header_digest(headers) != self.contract.header_sha256:
            raise BatchConflict("client sheet header fingerprint changed")
        if any(len(row) > len(self.contract.headers) for row in values[1:]):
            raise BatchConflict("client sheet contains data outside the validated header width")
        if any(not any(row) for row in values[1:-1]):
            raise BatchConflict(
                "client sheet contains an internal blank row; reconcile before release"
            )
        return [row + [""] * (len(self.contract.headers) - len(row)) for row in values]

    def _mutable_indices(self) -> set[int]:
        index = self.contract.column_map.get("Status")
        return {index} if index is not None else set()

    def _digest(self, values: list[list[str]]) -> str:
        mutable = self._mutable_indices()
        return digest(
            [
                [cell for index, cell in enumerate(row) if index not in mutable]
                for row in values
            ]
        )

    def _rows(self, frozen: list[dict[str, str]]) -> list[list[str]]:
        if len(frozen) != len(self.result.items):
            raise BatchConflict("frozen rows do not match prepared batch items")
        prepared = self.result.assembled_at.astimezone(UTC).isoformat()
        output = []
        for row, item in zip(frozen, self.result.items):
            values = {
                **row,
                "Status": "",
                "Batch ID": self.result.batch_id,
                "Batch Prepared At": prepared,
                "Job ID": item.representative_job_id,
            }
            target = [""] * len(self.contract.headers)
            for field, index in self.contract.column_map.items():
                target[index] = values[field]
            output.append(target)
        return output

    def plan(self, frozen: list[dict[str, str]]) -> tuple[str, str]:
        values = self._read()
        batch_index = self.contract.column_map.get("Batch ID")
        if batch_index is not None and any(
            len(row) > batch_index and row[batch_index] == self.result.batch_id
            for row in values[1:]
        ):
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
            raise BatchConflict("client sheet changed; reconcile before retry")
        self.gateway.append_table_rows(
            self.contract.spreadsheet_id,
            self.contract.worksheet_name,
            len(self.contract.headers),
            self._rows(frozen),
        )
        if self.inspect() != after:
            raise BatchConflict("client sheet append could not be verified; reconcile")


