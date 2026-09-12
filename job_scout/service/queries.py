"""Strict query grammar and stable in-memory ordering over bounded authorized evidence."""

from job_scout.service.errors import ServiceError

BASE = {"cursor", "snapshot_id", "limit"}
JOB = {"representation", "q", "decision", "source", "destination_id", "sort"}
JOB_RESERVED = {
    "brief_revision_id",
    "run_id",
    "cohort_id",
    "market",
    "work_mode",
    "outcome",
    "delivery_state",
    "from",
    "before",
}
HISTORY = {"q", "outcome", "source", "event_type", "destination_id", "sort"}
SORTS = {
    "jobs": {"first_seen_at", "company", "title", "decision"},
    "history": {"recorded_at"},
    "clients": {"display_name"},
    "briefs": {"registered_at"},
    "revisions": {"registered_at"},
}


def parse(params, route):
    family = route.split(":")[0]
    allowed = (
        BASE
        | {"sort"}
        | (
            {"q"}
            if family == "clients"
            else {"brief_id"}
            if family == "briefs"
            else JOB | JOB_RESERVED
            if family == "jobs"
            else HISTORY
            if family == "history"
            else set()
        )
    )
    if set(params) - allowed:
        raise ServiceError("VALIDATION_ERROR")
    if family == "jobs" and set(params) & JOB_RESERVED:
        raise ServiceError("EVIDENCE_SCOPE_UNAVAILABLE")
    result = {}
    for k in params:
        values = params.getlist(k)
        if len(values) > 1 and k not in {"decision", "source", "outcome", "event_type"}:
            raise ServiceError("VALIDATION_ERROR")
        result[k] = (
            tuple(sorted(set(values)))
            if k in {"decision", "source", "outcome", "event_type"}
            else values[0]
        )
    if "q" in result:
        result["q"] = result["q"].strip()
        if len(result["q"]) > 200:
            raise ServiceError("VALIDATION_ERROR")
    if "limit" in result:
        if result["limit"] not in {"20", "40", "80"}:
            raise ServiceError("VALIDATION_ERROR")
        result["limit"] = int(result["limit"])
    if "representation" in result and result["representation"] not in {"groups", "postings"}:
        raise ServiceError("VALIDATION_ERROR")
    for key, values in [
        ("decision", {"strong_match", "possible_match", "needs_review", "reject"}),
        ("outcome", {"applied", "not_applied", "unknown", "conflicting"}),
        ("event_type", {"imported_history", "delivery_event"}),
    ]:
        if key in result and not set(result[key]) <= values:
            raise ServiceError("VALIDATION_ERROR")
    if "sort" in result and result["sort"].removeprefix("-") not in SORTS.get(family, set()):
        raise ServiceError("VALIDATION_ERROR")
    return result


def defaults(route, query):
    q = {"limit": 40, **query}
    if route == "jobs":
        q.setdefault("representation", "groups")
        q.setdefault("decision", ("possible_match", "strong_match"))
    return q


def select(data, query, kind):
    def subject(item):
        return (
            item.representative_posting
            if getattr(item, "resource_type", None) == "delivery_group"
            else item
        )

    selected = []
    for item in data:
        p = subject(item)
        if kind == "jobs" and p is None:
            raise ServiceError("REPRESENTATION_UNAVAILABLE")
        text = " ".join(
            str(getattr(p, k, None) or "")
            for k in (
                ("title", "company", "location_text")
                if kind == "jobs"
                else ("title", "company", "original_url", "normalized_url")
                if kind == "history"
                else ("display_name",)
            )
        )
        if query.get("q", "").casefold() not in text.casefold():
            continue
        if "decision" in query and (p.match is None or p.match.decision not in query["decision"]):
            continue
        if "source" in query and getattr(p, "source", None) not in query["source"]:
            continue
        if "outcome" in query and p.outcome_summary.value not in query["outcome"]:
            continue
        if "event_type" in query and p.event_type not in query["event_type"]:
            continue
        if (
            kind == "history"
            and "destination_id" in query
            and p.destination_id != query["destination_id"]
        ):
            continue
        if "brief_id" in query and p.brief_id != query["brief_id"]:
            continue
        selected.append(item)

    def idkey(x):
        return next(
            getattr(x, k)
            for k in (
                "posting_id",
                "delivery_group_id",
                "history_entry_id",
                "brief_revision_id",
                "brief_id",
                "client_id",
            )
            if getattr(x, k, None)
        )

    selected.sort(key=idkey)
    priority = {"strong_match": 0, "possible_match": 1, "needs_review": 2, "reject": 3}

    def field(x, key):
        p = subject(x)
        if key == "decision":
            return priority.get(p.match.decision) if p and p.match else None
        if key == "recorded_at":
            return p.imported_at or p.exported_at
        value = getattr(p, key, None)
        return value.casefold() if isinstance(value, str) else value

    def order(key, desc):
        nonnull = [x for x in selected if field(x, key) is not None]
        null = [x for x in selected if field(x, key) is None]
        return sorted(nonnull, key=lambda x: field(x, key), reverse=desc) + null

    if query.get("sort"):
        selected = order(query["sort"].removeprefix("-"), query["sort"].startswith("-"))
    elif kind == "jobs":
        selected = order("first_seen_at", True)
        selected = sorted(
            selected, key=lambda x: field(x, "decision") if field(x, "decision") is not None else 4
        )
    else:
        selected = order(
            {
                "history": "recorded_at",
                "clients": "display_name",
                "briefs": "registered_at",
                "revisions": "registered_at",
            }[kind],
            kind != "clients",
        )
    return selected


def openapi_parameters(family):
    """Document the same bounded grammar used by parse(), without speculative operators."""
    names = BASE | (
        {"q", "sort"}
        if family == "clients"
        else JOB | JOB_RESERVED
        if family == "jobs"
        else HISTORY
        if family == "history"
        else {"brief_id", "sort"}
        if family == "briefs"
        else {"sort"}
        if family == "revisions"
        else set()
    )
    result = []
    for name in sorted(names):
        schema = {"type": "string"}
        if name == "limit":
            schema = {"type": "integer", "enum": [20, 40, 80], "default": 40}
        elif name == "q":
            schema = {"type": "string", "maxLength": 200}
        elif name == "representation":
            schema = {"type": "string", "enum": ["groups", "postings"], "default": "groups"}
        elif name == "sort":
            schema = {
                "type": "string",
                "enum": sorted(
                    SORTS.get(family, set()) | {"-" + k for k in SORTS.get(family, set())}
                ),
            }
        elif name in {"decision", "source", "outcome", "event_type"}:
            items = {"type": "string"}
            if name == "decision":
                items["enum"] = ["strong_match", "possible_match", "needs_review", "reject"]
            schema = {"type": "array", "items": items}
        result.append({"name": name, "in": "query", "required": False, "schema": schema})
    return result
