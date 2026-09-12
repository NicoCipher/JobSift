"""Validated stable public mappings supplied by provisioning, not allocated during GET."""

import hashlib
from urllib.parse import quote

from job_scout.domain.models import SearchBrief
from job_scout.service.config import ServiceConfig
from job_scout.service.errors import ServiceError
from job_scout.service.schemas import BriefProvenance, BriefRevision, Capability

READS = {"can_read_jobs", "can_read_history", "can_read_briefs"}
CAPABILITIES = READS | {
    "can_read_runs",
    "can_read_diagnostics",
    "can_edit_outcome",
    "can_create_brief_revision",
    "can_activate_brief",
    "can_start_run",
    "can_retry_run",
    "can_cancel_run",
    "can_view_raw_provenance",
    "can_manage_clients",
}


def capabilities():
    return {
        k: Capability(allowed=k in READS, reason=None if k in READS else "not_implemented")
        for k in sorted(CAPABILITIES)
    }


def url(client, suffix=""):
    return "/api/v1/clients/" + quote(client, safe="") + suffix


class Catalog:
    def __init__(self, config: ServiceConfig):
        config.check_development()
        self.config = config
        self.clients = {c.client_id: c for c in config.clients}
        self.destinations = {d.destination_id: d for d in config.destinations}
        self.histories = {h.history_entry_id: h for h in config.history}
        if (
            len(self.clients) != len(config.clients)
            or len(self.destinations) != len(config.destinations)
            or len(self.histories) != len(config.history)
            or not set(config.allowed_client_ids) <= self.clients.keys()
        ):
            raise ValueError("Invalid catalogue identities/grants")
        seen_history = set()
        for h in config.history:
            key = (h.client_id, h.historical_row_id, h.posting_id, h.destination_id)
            if key in seen_history:
                raise ValueError("Duplicate history mapping")
            seen_history.add(key)
            if h.destination_id and (
                h.destination_id not in self.destinations
                or self.destinations[h.destination_id].client_id != h.client_id
            ):
                raise ValueError("Invalid history destination binding")
        for r in (*config.destinations, *config.history, *config.briefs, *config.evidence):
            if r.client_id not in self.clients:
                raise ValueError("Unregistered client binding")
        self.briefs = {}
        lineages = {}
        for b in config.briefs:
            data = b.artifact_path.read_bytes()
            if hashlib.sha256(data).hexdigest() != b.content_sha256:
                raise ValueError("Registered brief hash mismatch")
            brief = SearchBrief.model_validate_json(data)
            if brief.client_id != b.client_id or b.brief_revision_id in self.briefs:
                raise ValueError("Invalid brief registration")
            if b.brief_id in lineages and lineages[b.brief_id] != b.client_id:
                raise ValueError("Conflicting lineage owner")
            lineages[b.brief_id] = b.client_id
            if b.registered_at.tzinfo is None:
                raise ValueError("Registration requires timezone")
            self.briefs[b.brief_revision_id] = BriefRevision(
                brief_revision_id=b.brief_revision_id,
                brief_id=b.brief_id,
                client_id=b.client_id,
                schema_version=brief.brief_version,
                revision_label=b.revision_label,
                content_sha256=b.content_sha256,
                registered_at=b.registered_at,
                rules=brief.model_dump(mode="json", exclude={"client_id", "brief_version"}),
                provenance=BriefProvenance(evidence_ref=b.brief_revision_id),
                capabilities=capabilities(),
            )
        for e in config.evidence:
            if hashlib.sha256(e.artifact_path.read_bytes()).hexdigest() != e.content_sha256:
                raise ValueError("Registered evidence hash mismatch")
        self.authorization_version = hashlib.sha256(config.model_dump_json().encode()).hexdigest()

    def authorize(self, client):
        if client not in self.config.allowed_client_ids or client not in self.clients:
            raise ServiceError("NOT_FOUND")

    def destination(self, client, id):
        if id is None:
            return None
        d = self.destinations.get(id)
        if d is None or d.client_id != client:
            raise ServiceError("NOT_FOUND")
        return d.domain_key
