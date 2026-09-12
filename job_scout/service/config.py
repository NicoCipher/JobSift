"""Explicit private configuration; no identities or artifact bindings inferred from paths."""

from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Registration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClientRegistration(Registration):
    client_id: str = Field(min_length=1)
    display_name: str


class DestinationRegistration(Registration):
    destination_id: str
    client_id: str
    display_name: str
    domain_key: str


class BriefRegistration(Registration):
    brief_id: str
    brief_revision_id: str
    client_id: str
    revision_label: str
    artifact_path: Path
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    registered_at: datetime


class HistoryRegistration(Registration):
    history_entry_id: str
    client_id: str
    historical_row_id: int | None = Field(default=None, gt=0)
    posting_id: str | None = None
    destination_id: str | None = None

    @model_validator(mode="after")
    def subject(self):
        if (self.historical_row_id is not None) == (self.posting_id is not None):
            raise ValueError("one explicit history subject required")
        if self.posting_id is not None and self.destination_id is None:
            raise ValueError("delivery history requires a destination registration")
        return self


class EvidenceRegistration(Registration):
    evidence_id: str
    client_id: str
    artifact_path: Path
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    kind: Literal["run", "diagnostics"]


class ServiceConfig(Registration):
    mode: Literal["development", "production"] = "production"
    trusted_development: bool = False
    bind_host: str = "127.0.0.1"
    origin: str = "http://127.0.0.1:8000"
    operator_id: str | None = None
    operator_display_name: str = "Development operator"
    allowed_client_ids: tuple[str, ...] = ()
    database_path: Path
    clients: tuple[ClientRegistration, ...] = ()
    destinations: tuple[DestinationRegistration, ...] = ()
    briefs: tuple[BriefRegistration, ...] = ()
    history: tuple[HistoryRegistration, ...] = ()
    evidence: tuple[EvidenceRegistration, ...] = ()
    max_projection_rows: int = Field(default=10000, ge=80, le=100000)
    max_projection_bytes: int = Field(default=16000000, ge=10000, le=64000000)

    def check_development(self):
        parsed = urlsplit(self.origin)
        if (
            self.mode != "development"
            or not self.trusted_development
            or not self.operator_id
            or not self.operator_id.strip()
            or not self.allowed_client_ids
            or "*" in self.allowed_client_ids
            or self.bind_host not in {"127.0.0.1", "::1", "localhost"}
            or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parsed.scheme != "http"
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError(
                "Explicit loopback trusted-development configuration required; production auth unavailable"
            )
