"""Bounded read-only SQLite evidence projection. No domain repository constructors."""

import ipaddress
import json
import sqlite3
from contextlib import contextmanager
from time import monotonic
from urllib.parse import quote, urlsplit

from job_scout.service.catalog import capabilities, url
from job_scout.service.errors import ServiceError
from job_scout.service.schemas import (
    ApplicationDestination,
    DeliveryState,
    Fact,
    Group,
    HistoryEntry,
    HistoryProvenance,
    MatchEvidence,
    Metric,
    OutcomeSummary,
    Posting,
    Provenance,
)


def safe_url(value, vacancy=False):
    if not value:
        return None
    value = str(value)
    try:
        p = urlsplit(value)
        host = p.hostname or ""
        if p.scheme != "https" or p.username or p.password or p.port not in {None, 443}:
            return None
        if (
            host.endswith((".localhost", ".local", ".internal"))
            or host == "localhost"
            or "." not in host
        ):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if all(x.isdigit() for x in host.split(".")):
                return None
        if vacancy and p.path.rstrip("/").lower() in {"", "/jobs", "/careers", "/apply"}:
            return None
        return value
    except ValueError:
        return None


def application(job):
    canonical = safe_url(job.get("canonical_url"))
    # Stored apply_url is provider evidence, never constructed by the service.
    direct = safe_url(job.get("apply_url"), vacancy=True)
    vacancy = safe_url(job.get("canonical_url"), vacancy=True)
    return ApplicationDestination(
        application_url=direct or vacancy,
        canonical_url=canonical,
        application_url_kind="direct_apply"
        if direct
        else "vacancy_page"
        if vacancy
        else "unavailable",
    )


class ReadStore:
    def __init__(self, catalog):
        self.catalog = catalog
        self.config = catalog.config

    @contextmanager
    def connect(self):
        c = None
        try:
            c = sqlite3.connect(
                self.config.database_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2
            )
            c.row_factory = sqlite3.Row
            c.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, self.config.max_projection_bytes)
            deadline = monotonic() + 5
            c.set_progress_handler(lambda: int(monotonic() > deadline), 1000)
            c.execute("PRAGMA query_only=ON")
            c.execute("BEGIN")
            yield c
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            raise ServiceError("EVIDENCE_UNAVAILABLE") from None
        finally:
            if c is not None:
                c.close()

    def bounded(self, c, sql, params=()):
        rows = c.execute(
            sql + " LIMIT ?", (*params, self.config.max_projection_rows + 1)
        ).fetchall()
        if len(rows) > self.config.max_projection_rows:
            raise ServiceError("EVIDENCE_SCOPE_UNAVAILABLE")
        return rows

    @staticmethod
    def has_table(c, name):
        return bool(
            c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
        )

    def outcome(self, c, client, type, id, baseline="unknown", baseline_ref=None):
        row = None
        if self.has_table(c, "operator_outcome_events"):
            row = c.execute(
                "SELECT event_id,value,version FROM operator_outcome_events WHERE client_id=? "
                "AND subject_type=? AND subject_id=? ORDER BY version DESC LIMIT 1",
                (client, type, id),
            ).fetchone()
        return OutcomeSummary(
            value=row["value"] if row else baseline,
            availability="reported",
            baseline_value=baseline,
            current_source="explicit_event"
            if row
            else "historical_import"
            if baseline_ref
            else "unknown",
            version=row["version"] if row else 0,
            latest_event_id=row["event_id"] if row else None,
            resolution="single" if row or baseline_ref else "not_recorded",
            evidence_refs=([baseline_ref] if baseline_ref else [])
            + ([row["event_id"]] if row else []),
        )

    def history_map(self, client):
        return {
            h.historical_row_id: h.history_entry_id
            for h in self.config.history
            if h.client_id == client and h.historical_row_id is not None
        }

    def delivery(self, c, client, group, job, destination, history):
        state = DeliveryState(destination_id=destination)
        if group and destination:
            key = self.catalog.destination(client, destination)
            rows = c.execute(
                "SELECT job_id FROM group_deliveries WHERE group_id=? AND client_id=? AND destination=?",
                (group, client, key),
            ).fetchall()
            state.previously_delivered = Fact(value=bool(rows), availability="reported")
            state.delivery_records = [
                h.history_entry_id
                for h in self.config.history
                if h.client_id == client
                and h.destination_id == destination
                and any(r[0] == h.posting_id for r in rows)
            ]
        # Same exact provider identity then canonical URL predicate as historical suppression.
        identity = (job["source"], job["source_board_id"], job["source_job_id"])
        matches = [
            r
            for r in history
            if (r["source"], r["source_board_id"], r["source_job_id"]) == identity
        ]
        basis = "provider_identity"
        if not matches:
            matches = [r for r in history if r["normalized_url"] == job["canonical_url"]]
            basis = "normalized_url"
        state.historical_suppression = Fact(value=bool(matches), availability="reported")
        mapping = self.history_map(client)
        state.historical_evidence_refs = [mapping[r["id"]] for r in matches if r["id"] in mapping]
        if matches:
            state.non_delivery_reasons = Fact(
                value=["historical_" + basis], availability="reported"
            )
        # Legacy current rows do not establish a verified fresh acquisition population.
        return state

    def jobs(self, client, destination=None):
        self.catalog.destination(client, destination)
        with self.connect() as c:
            history = self.bounded(
                c, "SELECT * FROM historical_job_links WHERE client_id=?", (client,)
            )
            rows = self.bounded(
                c,
                "SELECT j.* FROM jobs j WHERE EXISTS (SELECT 1 FROM job_matches m "
                "WHERE m.job_id=j.id AND m.client_id=?) OR EXISTS (SELECT 1 FROM exports e WHERE "
                "e.job_id=j.id AND e.client_id=?) OR EXISTS (SELECT 1 FROM group_deliveries d "
                "WHERE d.job_id=j.id AND d.client_id=?) ORDER BY j.id",
                (client, client, client),
            )
            posts = {}
            grouped = {}
            for r in rows:
                j = json.loads(r["payload_json"])
                m = c.execute(
                    "SELECT * FROM job_matches WHERE job_id=? AND client_id=?", (r["id"], client)
                ).fetchone()
                match = (
                    MatchEvidence(
                        decision=m["decision"],
                        matched_reasons=json.loads(m["matched_reasons_json"]),
                        rejection_reasons=json.loads(m["rejection_reasons_json"]),
                        matcher_version=m["matcher_version"],
                        evaluated_at=m["evaluated_at"],
                    )
                    if m
                    else None
                )
                g = (
                    c.execute(
                        "SELECT group_id FROM posting_delivery_groups WHERE job_id=?", (r["id"],)
                    ).fetchone()
                    if self.has_table(c, "posting_delivery_groups")
                    else None
                )
                group = g[0] if g else None
                post = Posting(
                    posting_id=r["id"],
                    source=j["source"],
                    source_board_id=j["source_board_id"],
                    source_job_id=j["source_job_id"],
                    title=j["title"],
                    company=j["company"],
                    location_text=j.get("location_text"),
                    remote_status=j["remote_status"],
                    first_seen_at=r["first_seen_at"],
                    last_seen_at=r["last_seen_at"],
                    application_destination=application(j),
                    match=match,
                    provenance=Provenance(
                        evidence_ref=r["id"],
                        source_target={"source": j["source"], "board": j["source_board_id"]},
                        first_seen_at=r["first_seen_at"],
                        last_seen_at=r["last_seen_at"],
                    ),
                    description_text=j.get("description_text"),
                    posted_at=j.get("posted_at"),
                    updated_at=j.get("updated_at"),
                    delivery_group_id=group,
                    delivery_state=self.delivery(c, client, group, j, destination, history),
                    outcome_summary=self.outcome(c, client, "posting", r["id"]),
                    capabilities=capabilities(),
                    detail_url=url(client, "/jobs/postings/" + quote(r["id"], safe="")),
                )
                posts[r["id"]] = post
                if group:
                    grouped.setdefault(group, []).append(post)
            groups = {}
            for id, members in grouped.items():
                rep = None
                if destination:
                    row = c.execute(
                        "SELECT job_id FROM group_deliveries WHERE group_id=? AND client_id=? AND destination=?",
                        (id, client, self.catalog.destination(client, destination)),
                    ).fetchone()
                    if row:
                        rep = posts.get(row[0])
                groups[id] = Group(
                    delivery_group_id=id,
                    representative_posting=rep,
                    representative_basis=Fact(value="recorded_delivery", availability="reported")
                    if rep
                    else Fact(),
                    member_count=Metric(
                        value=len(members),
                        unit="postings",
                        availability="reported",
                        definition="authorized_group_members",
                    ),
                    match=rep.match if rep else None,
                    delivery_state=rep.delivery_state
                    if rep
                    else DeliveryState(destination_id=destination),
                    detail_url=url(client, "/jobs/groups/" + quote(id, safe="")),
                    members_url=url(client, "/jobs/groups/" + quote(id, safe="") + "/postings"),
                    members=members,
                    capabilities=capabilities(),
                )
            return posts, groups

    def history(self, client):
        with self.connect() as c:
            rows = self.bounded(
                c,
                "SELECT h.*,i.workbook_sha256 FROM historical_job_links h JOIN historical_imports i "
                "ON h.import_id=i.id WHERE h.client_id=? ORDER BY h.id",
                (client,),
            )
            mappings = self.history_map(client)
            if any(r["id"] not in mappings for r in rows):
                raise ServiceError("EVIDENCE_SCOPE_UNAVAILABLE")
            entries = []
            for r in rows:
                id = mappings[r["id"]]
                entries.append(
                    HistoryEntry(
                        history_entry_id=id,
                        client_id=client,
                        event_type="imported_history",
                        operator_status=r["operator_status"],
                        original_url=safe_url(r["original_url"]),
                        normalized_url=safe_url(r["normalized_url"]),
                        source=r["source"],
                        source_board_id=r["source_board_id"],
                        source_job_id=r["source_job_id"],
                        title=r["title"],
                        company=r["company"],
                        imported_at=r["imported_at"],
                        provenance=HistoryProvenance(
                            import_id=r["import_id"],
                            workbook_sha256=r["workbook_sha256"],
                            sheet=r["source_sheet"],
                            row=r["source_row"],
                        ),
                        outcome_summary=self.outcome(
                            c, client, "history_entry", str(r["id"]), r["operator_status"], id
                        ),
                        capabilities=capabilities(),
                        detail_url=url(client, "/history/" + quote(id, safe="")),
                    )
                )
            exports = self.bounded(
                c,
                "SELECT e.job_id,e.destination,e.exported_at,j.payload_json,g.group_id FROM exports e JOIN jobs j ON j.id=e.job_id "
                "LEFT JOIN group_deliveries g ON g.job_id=e.job_id AND g.client_id=e.client_id AND g.destination=e.destination WHERE e.client_id=? "
                "UNION ALL SELECT d.job_id,d.destination,d.exported_at,j.payload_json,d.group_id FROM group_deliveries d "
                "JOIN jobs j ON j.id=d.job_id WHERE d.client_id=? AND NOT EXISTS (SELECT 1 FROM exports e "
                "WHERE e.job_id=d.job_id AND e.client_id=d.client_id AND e.destination=d.destination) ORDER BY job_id,destination",
                (client, client),
            )
            for r in exports:
                mapping = next(
                    (
                        h
                        for h in self.config.history
                        if h.client_id == client
                        and h.posting_id == r["job_id"]
                        and self.catalog.destination(client, h.destination_id) == r["destination"]
                    ),
                    None,
                )
                if mapping is None:
                    raise ServiceError("EVIDENCE_SCOPE_UNAVAILABLE")
                j = json.loads(r["payload_json"])
                entries.append(
                    HistoryEntry(
                        history_entry_id=mapping.history_entry_id,
                        client_id=client,
                        event_type="delivery_event",
                        operator_status="unknown",
                        posting_id=r["job_id"],
                        delivery_group_id=r["group_id"],
                        destination_id=mapping.destination_id,
                        title=j["title"],
                        company=j["company"],
                        source=j["source"],
                        source_board_id=j["source_board_id"],
                        source_job_id=j["source_job_id"],
                        exported_at=r["exported_at"],
                        outcome_summary=self.outcome(c, client, "posting", r["job_id"]),
                        capabilities=capabilities(),
                        detail_url=url(
                            client, "/history/" + quote(mapping.history_entry_id, safe="")
                        ),
                    )
                )
            return entries
