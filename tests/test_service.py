"""Service tests use only temporary SQLite/config artifacts and in-process ASGI requests."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from test_daily_batch import CLIENT, posting, seed
from test_operator_state import command, subject

from job_scout.domain.models import JobMatch, SearchBrief
from job_scout.history import HistoricalRecord
from job_scout.service.app import create_app
from job_scout.service.config import ServiceConfig
from job_scout.service.errors import STATUS, ErrorCode
from job_scout.storage.operator_state import OperatorStateStore
from job_scout.storage.sqlite import SQLiteRepository

BASE = "/api/v1/clients/" + CLIENT
ORIGIN = "http://127.0.0.1:8000"


@pytest.fixture
def setup(tmp_path):
    repo = SQLiteRepository(tmp_path / "domain.db")
    jobs = [posting(n) for n in range(1, 5)]
    seed(repo, jobs, ["strong_match", "possible_match", "needs_review", "reject"])
    hidden = posting(5, canonical_url=str(jobs[0].canonical_url))
    seed(repo, [hidden], client="hidden")
    for job in jobs:
        repo.mark_exported(job.id, CLIENT, str(tmp_path / "out.csv"))
    records = [
        HistoricalRecord(
            f"https://example.com/history/{n}",
            f"https://example.com/history/{n}",
            None,
            None,
            None,
            "Historical",
            "Example",
            value,
            "Sheet",
            n,
        )
        for n, value in enumerate(["applied", "not_applied", "unknown"], 1)
    ]
    repo.import_historical_records(client_id=CLIENT, workbook_sha256="a" * 64, records=records)
    state = OperatorStateStore(repo)
    with repo.connect() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM historical_job_links ORDER BY id")]
    state.record_outcome(command(subject=subject(str(ids[1]), "history_entry")))
    state.record_outcome(command(idempotency_key="posting-outcome-0001"))
    briefs = []
    for label in ("V1", "V2"):
        p = tmp_path / (label + ".json")
        p.write_text(
            SearchBrief(client_id=CLIENT, target_roles=["Support Engineer"]).model_dump_json()
        )
        briefs.append(
            {
                "brief_id": "lineage",
                "brief_revision_id": "revision-" + label,
                "client_id": CLIENT,
                "revision_label": label,
                "artifact_path": p,
                "content_sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                "registered_at": datetime(2026, 9, 1, tzinfo=UTC),
            }
        )
    config = ServiceConfig(
        mode="development",
        trusted_development=True,
        operator_id="operator",
        allowed_client_ids=(CLIENT,),
        database_path=tmp_path / "domain.db",
        clients=(
            {"client_id": CLIENT, "display_name": "Taiwo"},
            {"client_id": "hidden", "display_name": "Private"},
        ),
        destinations=(
            {
                "client_id": CLIENT,
                "destination_id": "destination",
                "display_name": "Delivery",
                "domain_key": str(tmp_path / "out.csv"),
            },
        ),
        briefs=tuple(briefs),
        history=tuple(
            [
                {"history_entry_id": f"history-{n}", "client_id": CLIENT, "historical_row_id": id}
                for n, id in enumerate(ids, 1)
            ]
            + [
                {
                    "history_entry_id": f"delivery-{n}",
                    "client_id": CLIENT,
                    "posting_id": f"job-{n}",
                    "destination_id": "destination",
                }
                for n in range(1, 5)
            ]
        ),
    )
    return repo, config


@pytest.fixture
def api(setup):
    app = create_app(setup[1])
    return TestClient(app, base_url=ORIGIN)


def test_disabled_defaults_and_production(setup):
    config = setup[1]
    for changes in (
        {"mode": "production"},
        {"trusted_development": False},
        {"operator_id": None},
        {"allowed_client_ids": ()},
        {"allowed_client_ids": ("*",)},
        {"bind_host": "0.0.0.0"},
    ):
        with pytest.raises(ValueError):
            create_app(config.model_copy(update=changes))
    with pytest.raises(ValueError):
        create_app(ServiceConfig(database_path=config.database_path))


@pytest.mark.parametrize(
    "headers", [{"host": "evil.example"}, {"origin": "https://evil.example"}, {"origin": "null"}]
)
def test_host_origin_rejected(api, headers):
    r = api.get("/api/v1/session", headers=headers)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


def test_session_headers_grants_and_capabilities(api):
    r = api.get("/api/v1/session", headers={"X-Operator-ID": "intruder", "X-Client-ID": "hidden"})
    data = r.json()["data"]
    assert data["operator_id"] == "operator"
    assert [c["client_id"] for c in data["client_scopes"]] == [CLIENT]
    for k, v in data["client_scopes"][0]["capabilities"].items():
        assert v["allowed"] == (k in {"can_read_jobs", "can_read_history", "can_read_briefs"})
    assert (
        data["authorization_version"]
        == api.get("/api/v1/session").json()["data"]["authorization_version"]
    )
    assert r.headers["cache-control"] == "no-store"
    assert "set-cookie" not in r.headers
    assert data["csrf_token"] is None
    assert api.get("/api/v1/clients").json()["page"]["known_total"]["value"] == 1


@pytest.mark.parametrize("suffix", ["", "/jobs", "/history", "/briefs", "/jobs/postings/job-5"])
def test_nested_scope_404(api, suffix):
    one = api.get("/api/v1/clients/hidden" + suffix)
    two = api.get("/api/v1/clients/absent" + suffix)
    assert one.status_code == two.status_code == 404
    assert one.json()["error"]["code"] == two.json()["error"]["code"] == "NOT_FOUND"


def test_database_read_only_and_no_new_schema(setup, api):
    repo, config = setup
    before = config.database_path.read_bytes()
    with repo.connect() as c:
        schema = [tuple(r) for r in c.execute("SELECT * FROM sqlite_master ORDER BY name")]
    for path in (
        "/api/v1/session",
        "/api/v1/clients",
        BASE,
        BASE + "/jobs?representation=postings",
        BASE + "/history",
        BASE + "/briefs",
        BASE + "/jobs/postings/job-1",
    ):
        assert api.get(path).status_code == 200
    assert config.database_path.read_bytes() == before
    with repo.connect() as c:
        assert [tuple(r) for r in c.execute("SELECT * FROM sqlite_master ORDER BY name")] == schema


def test_missing_db_redacted(setup, tmp_path):
    app = create_app(setup[1].model_copy(update={"database_path": tmp_path / "private-missing.db"}))
    r = TestClient(app, base_url=ORIGIN).get(BASE + "/jobs?representation=postings")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "EVIDENCE_UNAVAILABLE"
    assert "private-missing" not in r.text and str(tmp_path) not in r.text
    assert not (tmp_path / "private-missing.db").exists()


def test_sql_error_redacted(api, setup):
    with setup[0].connect() as c:
        c.execute("DROP TABLE job_matches")
    r = api.get(BASE + "/jobs?representation=postings")
    assert r.status_code == 503
    assert "SELECT" not in r.text and "job_matches" not in r.text


@pytest.mark.parametrize("decision", ["strong_match", "possible_match", "needs_review", "reject"])
def test_exact_match_decisions_and_scope(api, decision):
    r = api.get(BASE + "/jobs", params={"representation": "postings", "decision": decision})
    assert r.status_code == 200, r.text
    assert len(r.json()["data"]) == 1
    p = r.json()["data"][0]
    assert p["match"]["decision"] == decision
    assert p["posting_id"] != "job-5"
    assert p["match"]["brief_revision_id"] is None
    assert r.json()["meta"]["observed_at"] is None


def test_match_delivery_outcome_independent(api):
    p = api.get(BASE + "/jobs/postings/job-1?destination_id=destination").json()["data"]
    assert p["outcome_summary"]["value"] == "applied"
    assert p["match"]["decision"] == "strong_match"
    assert p["delivery_state"]["previously_delivered"] == {
        "value": True,
        "availability": "reported",
    }
    assert p["delivery_state"]["fresh_for_delivery"]["value"] is None
    other = api.get(BASE + "/jobs/postings/job-1").json()["data"]
    assert other["delivery_state"]["previously_delivered"]["availability"] == "not_reported"


def test_groups_privacy_and_representative(api, setup):
    r = api.get(BASE + "/jobs?destination_id=destination")
    assert r.status_code == 200, r.text
    groups = r.json()["data"]
    assert len(groups) == 2
    group = next(g for g in groups if g["representative_posting"]["posting_id"] == "job-1")
    assert group["member_count"]["value"] == 1
    assert group["member_completeness"] == "unknown"
    assert group["representative_basis"]["value"] == "recorded_delivery"
    assert group["outcome_summary"]["value"] is None
    assert "job-5" not in json.dumps(group)
    detail = api.get(group["detail_url"])
    assert detail.status_code == 200
    assert detail.json()["data"]["match"] == group["match"]
    assert api.get(group["members_url"]).status_code == 200
    assert api.get(BASE + "/jobs/postings/job-5").status_code == 404


def test_groups_without_representative_are_unavailable(api):
    r = api.get(BASE + "/jobs")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "REPRESENTATION_UNAVAILABLE"


@pytest.mark.parametrize(
    "value,kind",
    [
        ("https://example.com/direct/1", "direct_apply"),
        (None, "vacancy_page"),
        ("http://127.0.0.1/apply/1", "vacancy_page"),
        ("https://user:password@example.com/apply/1", "vacancy_page"),
        ("https://example.com/careers", "vacancy_page"),
    ],
)
def test_application_destination(api, setup, value, kind):
    job = posting(1, apply_url=value)
    setup[0].upsert_job(job)
    p = api.get(BASE + "/jobs/postings/job-1").json()["data"]
    assert p["application_destination"]["application_url_kind"] == kind
    assert p["application_destination"]["application_url"] == (
        value if kind == "direct_apply" else str(job.canonical_url)
    )


def test_html_text_inert(api, setup):
    setup[0].upsert_job(
        posting(
            1, description_text="<script>alert(1)</script>", description_html="<iframe>raw</iframe>"
        )
    )
    r = api.get(BASE + "/jobs/postings/job-1")
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["data"]["description_text"] == "<script>alert(1)</script>"
    assert "description_html" not in r.text and "<iframe>" not in r.text


def test_history_baselines_override_and_no_jobs(api, setup):
    r = api.get(BASE + "/history")
    assert r.status_code == 200, r.text
    imported = [h for h in r.json()["data"] if h["event_type"] == "imported_history"]
    assert {h["operator_status"] for h in imported} == {"applied", "not_applied", "unknown"}
    changed = next(h for h in imported if h["operator_status"] == "not_applied")
    assert changed["outcome_summary"]["value"] == "applied"
    assert changed["outcome_summary"]["baseline_value"] == "not_applied"
    assert changed["posting_id"] is None
    assert api.get(changed["detail_url"]).status_code == 200
    with setup[0].connect() as c:
        assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 5


def test_unregistered_history_fails_closed(setup):
    app = create_app(setup[1].model_copy(update={"history": ()}))
    r = TestClient(app, base_url=ORIGIN).get(BASE + "/history")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "EVIDENCE_SCOPE_UNAVAILABLE"


def test_brief_lineage_schema_and_hashes(api):
    result = api.get(BASE + "/briefs").json()
    assert result["page"]["known_total"]["unit"] == "briefs"
    assert len(result["data"]) == 1
    assert result["data"][0]["binding"]["value"] is None
    for label in ("V1", "V2"):
        r = api.get(BASE + "/briefs/revision-" + label)
        assert r.status_code == 200, r.text
        b = r.json()["data"]
        assert (
            b["client_id"] == CLIENT and b["schema_version"] == "operator-style-sourcing-brief-v1"
        )
        assert b["revision_label"] == label and b["binding"]["value"] is None
    assert api.get(BASE + "/briefs/unregistered-v2").status_code == 404


def test_brief_hash_mismatch_rejects_startup(setup):
    setup[1].briefs[0].artifact_path.write_text("{}")
    with pytest.raises(ValueError, match="hash mismatch"):
        create_app(setup[1])


@pytest.mark.parametrize("limit", [20, 40, 80])
def test_page_limits_and_reported_counts(api, limit):
    r = api.get(BASE + "/jobs", params={"representation": "postings", "limit": limit})
    assert r.status_code == 200
    assert r.json()["page"]["limit"] == limit
    assert r.json()["page"]["known_total"]["unit"] == "postings"
    assert r.json()["page"]["known_total"]["availability"] == "reported"


def test_default_limit_and_zero(api):
    r = api.get(BASE + "/jobs?representation=postings&q=nonexistent")
    assert r.json()["page"]["limit"] == 40
    assert r.json()["page"]["known_total"]["value"] == 0
    assert r.json()["data"] == []


@pytest.mark.parametrize(
    "params",
    [
        {"limit": "3"},
        {"unknown_filter": "x"},
        {"sort": "title; DROP TABLE jobs"},
        {"q": "x" * 201},
        {"decision": "applied"},
        {"representation": "mixed"},
        {"q[like]": "%"},
    ],
)
def test_typed_validation(api, params):
    r = api.get(BASE + "/jobs", params=params)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert r.json()["error"]["request_id"] == r.headers["x-request-id"]
    assert "DROP TABLE" not in r.text


def test_literal_search_not_wildcard(api):
    r = api.get(BASE + "/jobs?representation=postings&q=%25")
    assert r.status_code == 200 and r.json()["data"] == []


def test_revision_filter_conflict(api):
    r = api.get(BASE + "/jobs?brief_revision_id=revision-V2")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "EVIDENCE_SCOPE_UNAVAILABLE"
    assert not r.json()["error"]["retryable"]


def test_snapshot_rows_and_details_survive_changes(api, setup):
    repo = setup[0]
    seed(repo, [posting(n) for n in range(10, 55)])
    r = api.get(BASE + "/jobs?representation=postings&limit=20&sort=title")
    assert r.status_code == 200, r.text
    original = r.json()
    sid = original["page"]["snapshot_id"]
    row = original["data"][0]
    repo.save_match(
        JobMatch(
            job_id=row["posting_id"], client_id=CLIENT, decision="reject", matcher_version="changed"
        )
    )
    same = api.get(row["detail_url"])
    assert same.json()["data"]["match"] == row["match"]
    assert (
        api.get(BASE + "/jobs/postings/" + row["posting_id"]).json()["data"]["match"]["decision"]
        == "reject"
    )
    nxt = api.get(BASE + "/jobs", params={"cursor": original["page"]["next_cursor"]})
    assert nxt.status_code == 200 and nxt.json()["page"]["snapshot_id"] == sid
    previous = api.get(BASE + "/jobs", params={"cursor": nxt.json()["page"]["previous_cursor"]})
    assert previous.json()["data"] == original["data"]
    bad = api.get(
        BASE + "/jobs", params={"cursor": original["page"]["next_cursor"], "sort": "company"}
    )
    assert bad.status_code == 400


def test_snapshot_expiry_and_quota(api):
    first = api.get(BASE + "/jobs?representation=postings").json()
    for _ in range(4):
        assert api.get(BASE + "/jobs?representation=postings").status_code == 200
    assert api.get(BASE + "/jobs?representation=postings").status_code == 429
    api.app.state.snapshots.clock = lambda: datetime.now(UTC) + timedelta(minutes=31)
    r = api.get(first["data"][0]["detail_url"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "SNAPSHOT_EXPIRED"


def test_snapshot_cannot_cross_client_or_principal(setup):
    config = setup[1].model_copy(update={"allowed_client_ids": (CLIENT, "hidden")})
    app = create_app(config)
    api = TestClient(app, base_url=ORIGIN)
    sid = api.get(BASE + "/jobs?representation=postings").json()["page"]["snapshot_id"]
    assert api.get("/api/v1/clients/hidden/jobs", params={"snapshot_id": sid}).status_code == 400
    old = app.state.snapshots.items[sid]
    old.owner = ("another-operator", old.owner[1], old.owner[2])
    assert api.get(BASE + "/jobs", params={"snapshot_id": sid}).status_code == 400


def test_auth_version_binding_and_restart(api):
    r = api.get(BASE + "/jobs?representation=postings").json()
    sid = r["page"]["snapshot_id"]
    snap = api.app.state.snapshots.items[sid]
    snap.owner = (snap.owner[0], "old-grants", snap.owner[2])
    assert api.get(BASE + "/jobs", params={"snapshot_id": sid}).status_code == 400
    api.app.state.snapshots.items.clear()
    assert api.get(r["data"][0]["detail_url"]).json()["error"]["code"] == "SNAPSHOT_EXPIRED"


def test_openapi_and_no_writes(api):
    schema = api.get("/openapi.json").json()
    assert schema["openapi"].startswith("3.1")
    assert "/api/v1/clients/{client_id}/jobs" in schema["paths"]
    models = schema["components"]["schemas"]
    assert set(models["MatchDecision"]["enum"]) == {
        "strong_match",
        "possible_match",
        "needs_review",
        "reject",
    }
    assert set(models["ApplicationDestination"]["properties"]["application_url_kind"]["enum"]) == {
        "direct_apply",
        "vacancy_page",
        "unavailable",
    }
    assert all(set(v) <= {"get"} for v in schema["paths"].values())
    assert api.post(BASE + "/outcome-events", json={"value": "applied"}).status_code == 404


def test_error_code_mapping_and_health(api):
    assert STATUS[ErrorCode.EVIDENCE_SCOPE_UNAVAILABLE] == 409
    assert STATUS[ErrorCode.EVIDENCE_UNAVAILABLE] == 503
    assert api.get("/healthz").json() == {"status": "alive"}


def test_rate_admission(api):
    api.app.state.admission.tokens = 0
    api.app.state.admission.updated = 10**20
    r = api.get("/api/v1/session")
    assert r.status_code == 429 and "retry-after" in r.headers


def test_legacy_without_outcomes_or_group_mapping_stays_read_only(setup):
    repo, config = setup
    with repo.connect() as c:
        c.execute("DROP TABLE operator_outcome_imports")
        c.execute("DROP TABLE operator_outcome_events")
        c.execute("DROP TABLE posting_delivery_groups")
    before = config.database_path.read_bytes()
    api = TestClient(create_app(config), base_url=ORIGIN)
    r = api.get(BASE + "/jobs?representation=postings")
    assert r.status_code == 200, r.text
    assert r.json()["data"][0]["outcome_summary"]["value"] == "unknown"
    assert api.get(BASE + "/jobs").status_code == 409
    assert config.database_path.read_bytes() == before


def test_group_delivery_history_without_export_row(api, setup):
    with setup[0].connect() as c:
        c.execute("DELETE FROM exports WHERE job_id='job-1'")
    r = api.get(BASE + "/history")
    assert r.status_code == 200, r.text
    assert any(v["posting_id"] == "job-1" for v in r.json()["data"])


def test_empty_catalogue_collections_report_zero(setup):
    config = setup[1].model_copy(update={"briefs": ()})
    api = TestClient(create_app(config), base_url=ORIGIN)
    r = api.get(BASE + "/briefs")
    assert r.status_code == 200
    assert r.json()["page"]["known_total"]["value"] == 0
    assert r.json()["page"]["known_total"]["availability"] == "reported"


def test_unavailable_application_never_fabricates(api, setup):
    setup[0].upsert_job(posting(1, canonical_url="https://example.com/careers", apply_url=None))
    r = api.get(BASE + "/jobs/postings/job-1")
    assert r.json()["data"]["application_destination"]["application_url"] is None
    assert r.json()["data"]["application_destination"]["application_url_kind"] == "unavailable"


def test_snapshot_filter_limit_and_malformed_cursor(api, setup):
    seed(setup[0], [posting(n) for n in range(10, 40)])
    r = api.get(BASE + "/jobs?representation=postings&limit=20").json()
    cursor = r["page"]["next_cursor"]
    for params in (
        {"cursor": cursor, "q": "new"},
        {"cursor": cursor, "limit": 40},
        {"cursor": "not-a-handle"},
    ):
        response = api.get(BASE + "/jobs", params=params)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_CURSOR"


def test_openapi_queries_and_no_local_paths(api, setup):
    schema = api.get("/openapi.json").json()
    params = schema["paths"]["/api/v1/clients/{client_id}/jobs"]["get"]["parameters"]
    assert next(p for p in params if p["name"] == "limit")["schema"]["enum"] == [20, 40, 80]
    for path in (
        "/api/v1/session",
        BASE,
        BASE + "/history",
        BASE + "/jobs?representation=postings",
        BASE + "/briefs",
    ):
        r = api.get(path)
        assert r.status_code == 200
        assert str(setup[1].database_path.parent) not in r.text


def test_resource_etag_tracks_current_or_snapshot_evidence(api, setup):
    first = api.get(BASE + "/jobs?representation=postings").json()["data"][0]
    old = api.get(first["detail_url"])
    setup[0].save_match(
        JobMatch(
            job_id=first["posting_id"],
            client_id=CLIENT,
            decision="reject",
            matcher_version="updated",
        )
    )
    assert api.get(first["detail_url"]).headers["etag"] == old.headers["etag"]
    assert (
        api.get(BASE + "/jobs/postings/" + first["posting_id"]).headers["etag"]
        != old.headers["etag"]
    )
