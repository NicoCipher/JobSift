from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from job_scout.domain.models import Job
from job_scout.production_registry import ProductionSourceRegistry

INCREMENTAL_SOURCES = frozenset({"greenhouse", "smartrecruiters"})


class IncrementalTargetState(BaseModel):
    """Persisted provider identity evidence frozen into one refresh plan."""

    model_config = ConfigDict(extra="forbid")

    source: str
    board_id: str
    known_source_job_ids: list[str] = Field(default_factory=list)
    active_posted_at_by_source_job_id: dict[str, datetime] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_state(self) -> IncrementalTargetState:
        known = self.known_source_job_ids
        if known != sorted(set(known)):
            raise ValueError("known provider ids must be unique and sorted")
        if not set(self.active_posted_at_by_source_job_id).issubset(known):
            raise ValueError("active freshness evidence must belong to a known provider id")
        return self


def snapshot_incremental_target_state(
    repository,
    registry: ProductionSourceRegistry,
) -> list[IncrementalTargetState]:
    """Freeze identity/freshness memory without exposing the production DB to workers."""

    approved = {
        (target.source, target.source_target().board_id.strip())
        for target in registry.targets
        if target.source in INCREMENTAL_SOURCES
    }
    if not approved:
        return []

    known: dict[tuple[str, str], set[str]] = {key: set() for key in approved}
    active_posted: dict[tuple[str, str], dict[str, datetime]] = {
        key: {} for key in approved
    }

    with repository.connect() as connection:
        # Current jobs are the authoritative live crawler memory. They are not
        # copied to the long-term identity ledger until pruning, and normal
        # upserts intentionally clear job_retention_evidence.
        if getattr(connection, "is_postgres", False):
            # Planning only needs the provider identity plus posted_at. Let Neon
            # extract that scalar instead of sending every full job description.
            active_rows = connection.execute(
                "SELECT source,source_board_id,source_job_id,"
                "payload_json::jsonb->>'posted_at' AS posted_at "
                "FROM jobs "
                "WHERE source IN (?,?) "
                "ORDER BY source,source_board_id,source_job_id",
                tuple(sorted(INCREMENTAL_SOURCES)),
            ).fetchall()
            for row in active_rows:
                key = (row["source"], row["source_board_id"])
                if key not in approved:
                    continue
                source_job_id = row["source_job_id"]
                known[key].add(source_job_id)
                if row["posted_at"]:
                    active_posted[key][source_job_id] = datetime.fromisoformat(
                        row["posted_at"]
                    )
        else:
            active_rows = connection.execute(
                "SELECT source,source_board_id,source_job_id,payload_json "
                "FROM jobs "
                "WHERE source IN (?,?) "
                "ORDER BY source,source_board_id,source_job_id",
                tuple(sorted(INCREMENTAL_SOURCES)),
            ).fetchall()
            for row in active_rows:
                key = (row["source"], row["source_board_id"])
                if key not in approved:
                    continue
                source_job_id = row["source_job_id"]
                known[key].add(source_job_id)
                job = Job.model_validate_json(row["payload_json"])
                if job.posted_at is not None:
                    active_posted[key][source_job_id] = job.posted_at

        # The ledger remembers pruned identities, preventing repeated hydration
        # of postings whose age was already unknown/stale in earlier runs.
        rows = connection.execute(
            "SELECT source,source_board_id,source_job_id "
            "FROM job_identity_ledger "
            "WHERE source IN (?,?) "
            "ORDER BY source,source_board_id,source_job_id",
            tuple(sorted(INCREMENTAL_SOURCES)),
        ).fetchall()
        for row in rows:
            key = (row["source"], row["source_board_id"])
            if key in approved:
                known[key].add(row["source_job_id"])

    return [
        IncrementalTargetState(
            source=source,
            board_id=board_id,
            known_source_job_ids=sorted(known[(source, board_id)]),
            active_posted_at_by_source_job_id=dict(
                sorted(active_posted[(source, board_id)].items())
            ),
        )
        for source, board_id in sorted(approved)
    ]
