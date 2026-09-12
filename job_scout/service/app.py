"""Read-only FastAPI boundary. Factory startup requires explicit private configuration."""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from job_scout.service.auth import Admission
from job_scout.service.catalog import Catalog, capabilities, url
from job_scout.service.config import ServiceConfig
from job_scout.service.errors import STATUS, ErrorEnvelope, ServiceError, public_error
from job_scout.service.queries import defaults, openapi_parameters, parse, select
from job_scout.service.read_store import ReadStore
from job_scout.service.schemas import (
    Brief,
    BriefRevision,
    Client,
    ClientScope,
    Destination,
    Envelope,
    Group,
    HistoryEntry,
    ListEnvelope,
    Meta,
    Metric,
    PageInfo,
    Posting,
    Scope,
    Session,
)
from job_scout.service.snapshots import Snapshots


def load_config():
    path = Path(os.environ["JOBSIFT_SERVICE_CONFIG"]).resolve()
    data = json.loads(path.read_text())
    data["database_path"] = str(path.parent / data["database_path"])
    for key in ("briefs", "evidence"):
        for row in data.get(key, []):
            row["artifact_path"] = str(path.parent / row["artifact_path"])
    return ServiceConfig.model_validate(data)


def create_app(config: ServiceConfig | None = None):
    catalog = Catalog(config or load_config())
    config = catalog.config
    store = ReadStore(catalog)
    snapshots = Snapshots()
    admission = Admission(config)
    app = FastAPI(
        title="JobSift operator service V1",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        responses={status: {"model": ErrorEnvelope} for status in set(STATUS.values())},
    )
    app.state.snapshots = snapshots
    app.state.admission = admission
    app.state.catalog = catalog

    def error_response(request, code):
        value = public_error(code, request.state.request_id)
        headers = {"Retry-After": "30"} if code == "RATE_LIMITED" else {}
        return JSONResponse(
            value.model_dump(mode="json"), status_code=STATUS[value.error.code], headers=headers
        )

    @app.middleware("http")
    async def boundary(request, call_next):
        request.state.request_id = str(uuid4())
        entered = False
        try:
            admission.enter(request)
            entered = True
            response = await call_next(request)
        except ServiceError as e:
            response = error_response(request, e.code)
        except Exception:  # noqa: BLE001 -- public boundary must redact unexpected failures.
            response = error_response(request, "INTERNAL_ERROR")
        finally:
            if entered:
                admission.leave()
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(ServiceError)
    async def service_error(request, error):
        return error_response(request, error.code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return error_response(request, "VALIDATION_ERROR")

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        return error_response(
            request, "NOT_FOUND" if error.status_code == 404 else "VALIDATION_ERROR"
        )

    def meta(request, client=None, snapshot=None, complete=False, destination=None, revision=None):
        return Meta(
            request_id=request.state.request_id,
            scope=Scope(client_id=client, destination_id=destination, brief_revision_id=revision),
            served_at=datetime.now(UTC),
            snapshot_id=snapshot,
            completeness="complete" if complete else "unknown",
            limitations=["trusted_development_only", "observed_at_unreported"]
            + (
                []
                if complete
                else [
                    "match_revision_unattributed",
                    "acquisition_scope_unreported",
                    "freshness_unreported",
                    "authorized_members_only",
                ]
            ),
        )

    def owner(client):
        return config.operator_id, catalog.authorization_version, client

    def client_data(id):
        c = catalog.clients[id]
        return Client(
            client_id=id,
            display_name=c.display_name,
            detail_url=url(id),
            briefs_url=url(id, "/briefs"),
            destinations=[
                Destination(destination_id=d.destination_id, display_name=d.display_name)
                for d in config.destinations
                if d.client_id == id
            ],
            capabilities=capabilities(),
        )

    def build(client, route, q):
        details = {}
        if route == "clients":
            data = [client_data(id) for id in config.allowed_client_ids]
            unit = "clients"
        elif route == "jobs":
            posts, groups = store.jobs(client, q.get("destination_id"))
            details = {
                **{"posting:" + id: p for id, p in posts.items()},
                **{"group:" + id: g for id, g in groups.items()},
            }
            if q["representation"] == "groups" and any(
                p.delivery_group_id is None for p in posts.values()
            ):
                raise ServiceError("REPRESENTATION_UNAVAILABLE")
            data = list((posts if q["representation"] == "postings" else groups).values())
            unit = "postings" if q["representation"] == "postings" else "groups"
        elif route == "history":
            catalog.destination(client, q.get("destination_id"))
            data = store.history(client)
            details = {"history:" + p.history_entry_id: p for p in data}
            unit = "history_entries"
        else:
            revisions = [v for v in catalog.briefs.values() if v.client_id == client]
            details = {"brief:" + v.brief_revision_id: v for v in revisions}
            if route == "briefs":
                by_id = {}
                for v in revisions:
                    if v.brief_id not in by_id or by_id[v.brief_id].registered_at < v.registered_at:
                        by_id[v.brief_id] = Brief(
                            brief_id=v.brief_id,
                            client_id=client,
                            registered_at=v.registered_at,
                            revisions_url=url(
                                client, "/briefs/" + quote(v.brief_id, safe="") + "/revisions"
                            ),
                            capabilities=capabilities(),
                        )
                data = list(by_id.values())
                unit = "briefs"
            else:
                data = [v for v in revisions if v.brief_id == route.split(":", 1)[1]]
                if not data:
                    raise ServiceError("NOT_FOUND")
                unit = "revisions"
        selected = select(data, q, route.split(":")[0])
        size = sum(len(v.model_dump_json()) for v in [*selected, *details.values()])
        if len(selected) > config.max_projection_rows or size > config.max_projection_bytes:
            raise ServiceError("EVIDENCE_SCOPE_UNAVAILABLE")
        return selected, details, unit

    def link_snapshot(value, id):
        data = value.model_dump(mode="json")
        if isinstance(value, Client):
            return data

        def walk(node):
            if isinstance(node, dict):
                for key, v in node.items():
                    if key in {"detail_url", "members_url"} and v:
                        node[key] = v + "?snapshot_id=" + id
                    else:
                        walk(v)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(data)
        return data

    def listing(request, client, route):
        if client:
            catalog.authorize(client)
        supplied = parse(request.query_params, route)
        cursor = supplied.pop("cursor", None)
        id = supplied.pop("snapshot_id", None)
        position = 0
        if cursor:
            snap, position = snapshots.resolve(cursor, owner(client))
            if id and id != snap.id:
                raise ServiceError("INVALID_CURSOR")
        elif id:
            snap = snapshots.get(id, owner(client))
        else:
            q = defaults(route, supplied)
            data, details, unit = build(client, route, q)
            snap = snapshots.create(owner(client), route, q, data, details)
        if snap.route != route or any(snap.query.get(k) != v for k, v in supplied.items()):
            raise ServiceError("INVALID_CURSOR")
        unit = (
            ("postings" if snap.query["representation"] == "postings" else "groups")
            if route == "jobs"
            else "history_entries"
            if route == "history"
            else "revisions"
            if route.startswith("revisions:")
            else route
        )
        limit = snap.query["limit"]
        data = [link_snapshot(v, snap.id) for v in snap.data[position : position + limit]]
        page = PageInfo(
            limit=limit,
            previous_cursor=snapshots.cursor(snap, max(0, position - limit)) if position else None,
            next_cursor=snapshots.cursor(snap, position + limit)
            if position + limit < len(snap.data)
            else None,
            known_total=Metric(
                value=len(snap.data),
                availability="reported",
                unit=unit,
                definition="authorized_" + unit + "_in_scope",
            ),
            snapshot_id=snap.id,
            expires_at=snap.expires,
        )
        m = meta(
            request,
            client,
            snap.id,
            route not in {"jobs", "history"},
            snap.query.get("destination_id"),
        )
        if route == "jobs":
            m.supported_filters = ["q", "decision", "source", "destination_id"]
            m.supported_sorts = ["decision", "first_seen_at", "company", "title"]
        return {"data": data, "meta": m, "page": page}

    def detail(request, client, kind, id, response):
        catalog.authorize(client)
        allowed = (
            {"snapshot_id", "destination_id"} if kind in {"posting", "group"} else {"snapshot_id"}
        )
        if set(request.query_params) - allowed or any(
            len(request.query_params.getlist(k)) != 1 for k in request.query_params
        ):
            raise ServiceError("VALIDATION_ERROR")
        sid = request.query_params.get("snapshot_id")
        destination = request.query_params.get("destination_id")
        if sid:
            snap = snapshots.get(sid, owner(client))
            if destination and destination != snap.query.get("destination_id"):
                raise ServiceError("INVALID_CURSOR")
            destination = snap.query.get("destination_id")
            item = snap.details.get(kind + ":" + id)
            if item is None:
                raise ServiceError("REPRESENTATION_UNAVAILABLE")
        else:
            if kind in {"posting", "group"}:
                posts, groups = store.jobs(client, destination)
                item = (posts if kind == "posting" else groups).get(id)
            elif kind == "history":
                item = next((v for v in store.history(client) if v.history_entry_id == id), None)
            else:
                item = catalog.briefs.get(id)
                if item and item.client_id != client:
                    item = None
            if item is None:
                raise ServiceError("NOT_FOUND")
        response.headers["ETag"] = (
            '"' + hashlib.sha256(item.model_dump_json().encode()).hexdigest() + '"'
        )
        result = {
            "data": link_snapshot(item, sid) if sid else item,
            "meta": meta(
                request, client, sid, kind == "brief", destination, id if kind == "brief" else None
            ),
        }
        return result

    @app.get("/healthz")
    def health():
        return {"status": "alive"}

    @app.get("/api/v1/session", response_model=Envelope[Session])
    def session(request: Request):
        if request.query_params:
            raise ServiceError("VALIDATION_ERROR")
        return Envelope(
            data=Session(
                operator_id=config.operator_id,
                display_name=config.operator_display_name,
                authorization_version=catalog.authorization_version,
                capabilities={"can_manage_clients": capabilities()["can_manage_clients"]},
                client_scopes=[
                    ClientScope(client_id=id, capabilities=capabilities())
                    for id in config.allowed_client_ids
                ],
            ),
            meta=meta(request, complete=True),
        )

    @app.get(
        "/api/v1/clients",
        response_model=ListEnvelope[Client],
        openapi_extra={"parameters": openapi_parameters("clients")},
    )
    def clients(request: Request):
        return listing(request, None, "clients")

    @app.get("/api/v1/clients/{client_id}", response_model=Envelope[Client])
    def client(request: Request, client_id: str):
        catalog.authorize(client_id)
        if request.query_params:
            raise ServiceError("VALIDATION_ERROR")
        return Envelope(data=client_data(client_id), meta=meta(request, client_id, complete=True))

    @app.get(
        "/api/v1/clients/{client_id}/jobs",
        response_model=ListEnvelope[Posting | Group],
        openapi_extra={"parameters": openapi_parameters("jobs")},
    )
    def jobs(request: Request, client_id: str):
        return listing(request, client_id, "jobs")

    @app.get(
        "/api/v1/clients/{client_id}/jobs/postings/{posting_id}", response_model=Envelope[Posting]
    )
    def posting(request: Request, response: Response, client_id: str, posting_id: str):
        return detail(request, client_id, "posting", posting_id, response)

    @app.get(
        "/api/v1/clients/{client_id}/jobs/groups/{delivery_group_id}",
        response_model=Envelope[Group],
        openapi_extra={
            "parameters": [
                {"name": n, "in": "query", "required": False, "schema": {"type": "string"}}
                for n in ("snapshot_id", "destination_id")
            ]
        },
    )
    def group(request: Request, response: Response, client_id: str, delivery_group_id: str):
        return detail(request, client_id, "group", delivery_group_id, response)

    @app.get(
        "/api/v1/clients/{client_id}/jobs/groups/{delivery_group_id}/postings",
        response_model=ListEnvelope[Posting],
        openapi_extra={"parameters": openapi_parameters("members")},
    )
    def members(request: Request, client_id: str, delivery_group_id: str):
        catalog.authorize(client_id)
        supplied = parse(request.query_params, "members")
        cursor = supplied.pop("cursor", None)
        sid = supplied.pop("snapshot_id", None)
        route = "members:" + delivery_group_id
        position = 0
        if cursor:
            snap, position = snapshots.resolve(cursor, owner(client_id))
            if sid and sid != snap.id:
                raise ServiceError("INVALID_CURSOR")
        else:
            if sid:
                original = snapshots.get(sid, owner(client_id))
                group = original.details.get("group:" + delivery_group_id)
                if group is None:
                    raise ServiceError("REPRESENTATION_UNAVAILABLE")
            else:
                _, groups = store.jobs(client_id)
                group = groups.get(delivery_group_id)
                if group is None:
                    raise ServiceError("NOT_FOUND")
            q = defaults(route, supplied)
            data = sorted(group.members, key=lambda p: p.posting_id)
            snap = snapshots.create(
                owner(client_id), route, q, data, {"posting:" + p.posting_id: p for p in data}
            )
        if snap.route != route or any(snap.query.get(k) != v for k, v in supplied.items()):
            raise ServiceError("INVALID_CURSOR")
        limit = snap.query["limit"]
        return {
            "data": [link_snapshot(p, snap.id) for p in snap.data[position : position + limit]],
            "meta": meta(request, client_id, snap.id),
            "page": PageInfo(
                limit=limit,
                next_cursor=snapshots.cursor(snap, position + limit)
                if position + limit < len(snap.data)
                else None,
                previous_cursor=snapshots.cursor(snap, max(0, position - limit))
                if position
                else None,
                known_total=Metric(
                    value=len(snap.data),
                    unit="postings",
                    availability="reported",
                    definition="authorized_group_members",
                ),
                snapshot_id=snap.id,
                expires_at=snap.expires,
            ),
        }

    @app.get(
        "/api/v1/clients/{client_id}/history",
        response_model=ListEnvelope[HistoryEntry],
        openapi_extra={"parameters": openapi_parameters("history")},
    )
    def history(request: Request, client_id: str):
        return listing(request, client_id, "history")

    @app.get(
        "/api/v1/clients/{client_id}/history/{history_entry_id}",
        response_model=Envelope[HistoryEntry],
        openapi_extra={
            "parameters": [
                {"name": n, "in": "query", "required": False, "schema": {"type": "string"}}
                for n in ("snapshot_id", "destination_id")
            ]
        },
    )
    def history_detail(request: Request, response: Response, client_id: str, history_entry_id: str):
        return detail(request, client_id, "history", history_entry_id, response)

    @app.get(
        "/api/v1/clients/{client_id}/briefs",
        response_model=ListEnvelope[Brief],
        openapi_extra={"parameters": openapi_parameters("briefs")},
    )
    def briefs(request: Request, client_id: str):
        return listing(request, client_id, "briefs")

    @app.get(
        "/api/v1/clients/{client_id}/briefs/{brief_id}/revisions",
        response_model=ListEnvelope[BriefRevision],
        openapi_extra={"parameters": openapi_parameters("revisions")},
    )
    def revisions(request: Request, client_id: str, brief_id: str):
        return listing(request, client_id, "revisions:" + brief_id)

    @app.get(
        "/api/v1/clients/{client_id}/briefs/{brief_revision_id}",
        response_model=Envelope[BriefRevision],
        openapi_extra={
            "parameters": [
                {"name": n, "in": "query", "required": False, "schema": {"type": "string"}}
                for n in ("snapshot_id", "destination_id")
            ]
        },
    )
    def brief(request: Request, response: Response, client_id: str, brief_revision_id: str):
        return detail(request, client_id, "brief", brief_revision_id, response)

    return app
