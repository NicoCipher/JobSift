from __future__ import annotations

from job_scout.export.batch_sheets import GoogleSheetsGateway


class Response:
    def __init__(self, payload=None):
        self.payload = payload or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class Session:
    def __init__(self):
        self.posts = []
        self.puts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return Response()

    def put(self, url, **kwargs):
        self.puts.append((url, kwargs))
        return Response()


def gateway(metadata, rows):
    value = GoogleSheetsGateway.__new__(GoogleSheetsGateway)
    value.session = Session()
    value.sheet_metadata = lambda spreadsheet_id: metadata
    value.read_rows = lambda spreadsheet_id, tab: rows
    return value


def test_creates_missing_tab_and_writes_headers_only_to_empty_sheet():
    value = gateway([], [])
    headers = ["Job Title", "Company Name", "Job Link", "Status", "Batch ID"]

    value.ensure_client_sheet("sheet123", "Jobs", headers)

    assert len(value.session.posts) == 1
    assert value.session.posts[0][1]["json"]["requests"][0]["addSheet"]["properties"]["title"] == "Jobs"
    assert len(value.session.puts) == 1
    assert value.session.puts[0][1]["json"]["values"] == [headers]


def test_initializes_existing_empty_tab_without_recreating_it():
    value = gateway([{"sheet_id": 12, "title": "Jobs"}], [])
    headers = ["Job Title", "Company Name", "Job Link", "Status", "Batch ID"]

    value.ensure_client_sheet("sheet123", "Jobs", headers)

    assert value.session.posts == []
    assert len(value.session.puts) == 1
    assert value.session.puts[0][1]["json"]["values"] == [headers]


def test_preserves_existing_sheet_contents_and_headers():
    existing = [["Role", "Company", "URL"], ["Engineer", "Acme", "https://example.com"]]
    value = gateway([{"sheet_id": 12, "title": "Jobs"}], existing)

    value.ensure_client_sheet("sheet123", "Jobs", ["Job Title", "Company Name", "Job Link"])

    assert value.session.posts == []
    assert value.session.puts == []
