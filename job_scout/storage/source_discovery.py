"""Persistent state for live ATS source discovery and health-gated admission."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from job_scout.production_registry import ProductionTarget
from job_scout.storage.sqlite import SQLiteRepository

SOURCE_DISCOVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_discovery_targets (
  target_identity TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  coordinates_json TEXT NOT NULL,
  company_hint TEXT NOT NULL,
  first_discovered_at TEXT NOT NULL,
  last_discovered_at TEXT NOT NULL,
  last_discovery_source TEXT NOT NULL,
  last_discovery_crawl_id TEXT NOT NULL,
  last_discovery_url TEXT NOT NULL,
  latest_health_classification TEXT,
  latest_health_checked_at TEXT,
  latest_health_evidence_sha256 TEXT,
  health_current_postings INTEGER,
  health_inventory_exact INTEGER
    CHECK(health_inventory_exact IN (0,1) OR health_inventory_exact IS NULL),
  health_error TEXT,
  admitted INTEGER NOT NULL DEFAULT 0 CHECK(admitted IN (0,1)),
  admitted_at TEXT,
  next_health_check_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_source_discovery_targets_health_due
  ON source_discovery_targets(next_health_check_at, latest_health_checked_at);
CREATE INDEX IF NOT EXISTS ix_source_discovery_targets_admitted
  ON source_discovery_targets(admitted, latest_health_checked_at);

CREATE TABLE IF NOT EXISTS source_discovery_evidence (
  evidence_id TEXT PRIMARY KEY,
  target_identity TEXT NOT NULL
    REFERENCES source_discovery_targets(target_identity) ON DELETE CASCADE,
  discovery_source TEXT NOT NULL,
  crawl_id TEXT NOT NULL,
  query_id TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  discovered_url TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  evidence_sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_source_discovery_evidence_target
  ON source_discovery_evidence(target_identity, observed_at);

CREATE TABLE IF NOT EXISTS source_discovery_health_evidence (
  evidence_id TEXT PRIMARY KEY,
  target_identity TEXT NOT NULL
    REFERENCES source_discovery_targets(target_identity) ON DELETE CASCADE,
  checked_at TEXT NOT NULL,
  classification TEXT NOT NULL,
  current_postings INTEGER,
  inventory_exact INTEGER CHECK(inventory_exact IN (0,1) OR inventory_exact IS NULL),
  http_status INTEGER,
  request_count INTEGER NOT NULL,
  error TEXT,
  evidence_json TEXT NOT NULL,
  evidence_sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_source_discovery_health_target
  ON source_discovery_health_evidence(target_identity, checked_at);

CREATE TABLE IF NOT EXISTS source_discovery_cursors (
  discovery_source TEXT NOT NULL,
  query_id TEXT NOT NULL,
  crawl_id TEXT NOT NULL,
  page INTEGER NOT NULL CHECK(page >= 0),
  pages INTEGER NOT NULL CHECK(pages >= 1),
  updated_at TEXT NOT NULL,
  PRIMARY KEY(discovery_source, query_id)
);
"""


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _health_interval(classification: str) -> timedelta:
    if classification in {"active", "valid_empty"}:
        return timedelta(hours=24)
    if classification in {"invalid", "unprocessable"}:
        return timedelta(days=7)
    if classification == "restricted":
        return timedelta(days=3)
    return timedelta(hours=6)


def _begin_write(connection) -> None:
    """Start a discovery write without taking JobSift's global Postgres lock."""

    # SQLite still needs BEGIN IMMEDIATE for local writer serialization.
    # Production Postgres uses MVCC and constraints for these isolated
    # source_discovery_* tables; the discovery workflow itself is singleton.
    statement = "BEGIN" if getattr(connection, "is_postgres", False) else "BEGIN IMMEDIATE"
    connection.execute(statement)


class SourceDiscoveryStore:
    """Auditable candidate, health-evidence, admission and crawl-cursor state."""

    def __init__(self, repository: SQLiteRepository):
        self.repository = repository
        with repository.connect() as connection:
            connection.executescript(SOURCE_DISCOVERY_SCHEMA)

    def get_cursor(self, *, discovery_source: str, query_id: str) -> dict[str, Any] | None:
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT crawl_id,page,pages,updated_at FROM source_discovery_cursors "
                "WHERE discovery_source=? AND query_id=?",
                (discovery_source, query_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "crawl_id": row["crawl_id"],
            "page": row["page"],
            "pages": row["pages"],
            "updated_at": row["updated_at"],
        }

    def set_cursor(
        self,
        *,
        discovery_source: str,
        query_id: str,
        crawl_id: str,
        page: int,
        pages: int,
        updated_at: datetime,
    ) -> None:
        if page < 0 or pages < 1 or page >= pages:
            raise ValueError("invalid source-discovery cursor")
        with self.repository.connect() as connection:
            _begin_write(connection)
            connection.execute(
                "INSERT INTO source_discovery_cursors "
                "(discovery_source,query_id,crawl_id,page,pages,updated_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(discovery_source,query_id) DO UPDATE SET "
                "crawl_id=excluded.crawl_id,page=excluded.page,pages=excluded.pages,"
                "updated_at=excluded.updated_at",
                (
                    discovery_source,
                    query_id,
                    crawl_id,
                    page,
                    pages,
                    _aware(updated_at).isoformat(),
                ),
            )

    @staticmethod
    def _observe_one(connection, value: dict[str, Any]) -> tuple[bool, bool]:
        observed = _aware(value["observed_at"]).isoformat()
        coordinates = value["coordinates"]
        coordinates_json = _canonical(coordinates)
        target_identity = value["target_identity"]
        source = value["source"]
        evidence_payload = {
            "target_identity": target_identity,
            "source": source,
            "coordinates": coordinates,
            "discovery_source": value["discovery_source"],
            "crawl_id": value["crawl_id"],
            "query_id": value["query_id"],
            "captured_at": value["captured_at"],
            "discovered_url": value["discovered_url"],
        }
        evidence_sha = _digest(evidence_payload)

        existing = connection.execute(
            "SELECT source,coordinates_json FROM source_discovery_targets "
            "WHERE target_identity=?",
            (target_identity,),
        ).fetchone()
        if existing is not None and (
            existing["source"] != source
            or existing["coordinates_json"] != coordinates_json
        ):
            raise ValueError("discovered target identity changed source coordinates")

        inserted_target = (
            connection.execute(
                "INSERT OR IGNORE INTO source_discovery_targets "
                "(target_identity,source,coordinates_json,company_hint,"
                "first_discovered_at,last_discovered_at,last_discovery_source,"
                "last_discovery_crawl_id,last_discovery_url) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    target_identity,
                    source,
                    coordinates_json,
                    value["company_hint"],
                    observed,
                    observed,
                    value["discovery_source"],
                    value["crawl_id"],
                    value["discovered_url"],
                ),
            ).rowcount
            == 1
        )
        if not inserted_target:
            connection.execute(
                "UPDATE source_discovery_targets SET "
                "last_discovered_at=?,last_discovery_source=?,"
                "last_discovery_crawl_id=?,last_discovery_url=? "
                "WHERE target_identity=?",
                (
                    observed,
                    value["discovery_source"],
                    value["crawl_id"],
                    value["discovered_url"],
                    target_identity,
                ),
            )

        inserted_evidence = (
            connection.execute(
                "INSERT OR IGNORE INTO source_discovery_evidence "
                "(evidence_id,target_identity,discovery_source,crawl_id,query_id,"
                "captured_at,discovered_url,observed_at,evidence_sha256) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    evidence_sha,
                    target_identity,
                    value["discovery_source"],
                    value["crawl_id"],
                    value["query_id"],
                    value["captured_at"],
                    value["discovered_url"],
                    observed,
                    evidence_sha,
                ),
            ).rowcount
            == 1
        )
        return inserted_target, inserted_evidence

    def observe_candidates(self, values: list[dict[str, Any]]) -> tuple[int, int]:
        """Persist a discovery page in one transaction and one remote sync."""
        if not values:
            return 0, 0
        inserted_targets = 0
        inserted_evidence = 0
        with self.repository.connect() as connection:
            _begin_write(connection)
            for value in values:
                target_added, evidence_added = self._observe_one(connection, value)
                inserted_targets += int(target_added)
                inserted_evidence += int(evidence_added)
        return inserted_targets, inserted_evidence

    def observe_candidate(
        self,
        *,
        target_identity: str,
        source: str,
        coordinates: dict[str, str],
        company_hint: str,
        discovery_source: str,
        crawl_id: str,
        query_id: str,
        captured_at: str,
        discovered_url: str,
        observed_at: datetime,
    ) -> tuple[bool, bool]:
        return tuple(
            bool(value)
            for value in self.observe_candidates(
                [
                    {
                        "target_identity": target_identity,
                        "source": source,
                        "coordinates": coordinates,
                        "company_hint": company_hint,
                        "discovery_source": discovery_source,
                        "crawl_id": crawl_id,
                        "query_id": query_id,
                        "captured_at": captured_at,
                        "discovered_url": discovered_url,
                        "observed_at": observed_at,
                    }
                ]
            )
        )

    def health_candidates(
        self,
        *,
        now: datetime,
        limit: int,
        admission_max_age_hours: int = 48,
        expiry_guard_hours: int = 6,
    ) -> list[dict[str, Any]]:
        """Select due health work without starving either admission or rechecks.

        Previously admitted active targets that are close to aging out of the
        admission window always win. Outside that guard window, half of a
        multi-target batch is reserved for never-checked discovery candidates;
        routine rechecks and retries consume the other half, then either side
        may use spare capacity.
        """
        if limit < 1:
            raise ValueError("health candidate limit must be positive")
        if admission_max_age_hours < 1:
            raise ValueError("admission health age must be positive")
        if expiry_guard_hours < 0:
            raise ValueError("health expiry guard cannot be negative")

        effective_guard_hours = min(
            expiry_guard_hours,
            max(admission_max_age_hours - 1, 0),
        )
        current_dt = _aware(now)
        current = current_dt.isoformat()
        urgent_cutoff = (
            current_dt
            - timedelta(hours=admission_max_age_hours - effective_guard_hours)
        ).isoformat()
        select_columns = (
            "SELECT target_identity,source,coordinates_json,company_hint,"
            "latest_health_classification,latest_health_checked_at,"
            "admitted,admitted_at,first_discovered_at,next_health_check_at "
            "FROM source_discovery_targets "
        )
        due_clause = (
            "WHERE (next_health_check_at IS NULL OR next_health_check_at<=?) "
        )
        with self.repository.connect() as connection:
            urgent = connection.execute(
                select_columns
                + due_clause
                + "AND (admitted=1 OR admitted_at IS NOT NULL) "
                "AND latest_health_classification='active' "
                "AND latest_health_checked_at IS NOT NULL "
                "AND latest_health_checked_at<=? "
                "ORDER BY latest_health_checked_at,target_identity LIMIT ?",
                (current, urgent_cutoff, limit),
            ).fetchall()
            unchecked = connection.execute(
                select_columns
                + due_clause
                + "AND latest_health_checked_at IS NULL "
                "ORDER BY first_discovered_at,target_identity LIMIT ?",
                (current, limit),
            ).fetchall()
            routine = connection.execute(
                select_columns
                + due_clause
                + "AND latest_health_checked_at IS NOT NULL "
                "AND NOT ("
                "(admitted=1 OR admitted_at IS NOT NULL) "
                "AND latest_health_classification='active' "
                "AND latest_health_checked_at<=?"
                ") "
                "ORDER BY "
                "CASE WHEN admitted=1 OR admitted_at IS NOT NULL THEN 0 ELSE 1 END,"
                "COALESCE(next_health_check_at,''),target_identity LIMIT ?",
                (current, urgent_cutoff, limit),
            ).fetchall()

        selected: list[tuple[Any, str]] = [
            (row, "expiry_guard") for row in urgent[:limit]
        ]
        remaining = limit - len(selected)
        reserve = min(remaining, limit // 2, len(unchecked))
        selected.extend((row, "new_candidate") for row in unchecked[:reserve])
        remaining = limit - len(selected)

        if remaining:
            selected.extend((row, "routine") for row in routine[:remaining])
            remaining = limit - len(selected)
        if remaining:
            selected.extend(
                (row, "new_candidate")
                for row in unchecked[reserve : reserve + remaining]
            )

        return [
            {
                "target_identity": row["target_identity"],
                "source": row["source"],
                "coordinates": json.loads(row["coordinates_json"]),
                "company_hint": row["company_hint"],
                "latest_health_classification": row["latest_health_classification"],
                "latest_health_checked_at": row["latest_health_checked_at"],
                "ever_admitted": row["admitted_at"] is not None,
                "health_selection_reason": reason,
            }
            for row, reason in selected
        ]

    @staticmethod
    def _record_health_one(
        connection,
        *,
        target_identity: str,
        checked_at: datetime,
        result: dict[str, Any],
    ) -> None:
        checked = _aware(checked_at)
        classification = str(result["classification"])
        admitted = classification == "active"
        next_check = checked + _health_interval(classification)
        evidence_payload = {
            "target_identity": target_identity,
            "checked_at": checked.isoformat(),
            **result,
        }
        evidence_json = _canonical(evidence_payload)
        evidence_sha = hashlib.sha256(evidence_json.encode()).hexdigest()

        if connection.execute(
            "SELECT 1 FROM source_discovery_targets WHERE target_identity=?",
            (target_identity,),
        ).fetchone() is None:
            raise ValueError("cannot health-check an unknown discovered target")

        connection.execute(
            "INSERT OR IGNORE INTO source_discovery_health_evidence "
            "(evidence_id,target_identity,checked_at,classification,current_postings,"
            "inventory_exact,http_status,request_count,error,evidence_json,evidence_sha256) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                evidence_sha,
                target_identity,
                checked.isoformat(),
                classification,
                result.get("current_postings"),
                (
                    None
                    if result.get("inventory_exact") is None
                    else int(bool(result.get("inventory_exact")))
                ),
                result.get("http_status"),
                int(result.get("request_count") or 0),
                result.get("error"),
                evidence_json,
                evidence_sha,
            ),
        )
        connection.execute(
            "UPDATE source_discovery_targets SET "
            "latest_health_classification=?,latest_health_checked_at=?,"
            "latest_health_evidence_sha256=?,health_current_postings=?,"
            "health_inventory_exact=?,health_error=?,"
            "admitted=?,admitted_at=CASE WHEN ?=1 THEN COALESCE(admitted_at,?) "
            "ELSE admitted_at END,next_health_check_at=? "
            "WHERE target_identity=?",
            (
                classification,
                checked.isoformat(),
                evidence_sha,
                result.get("current_postings"),
                (
                    None
                    if result.get("inventory_exact") is None
                    else int(bool(result.get("inventory_exact")))
                ),
                result.get("error"),
                int(admitted),
                int(admitted),
                checked.isoformat(),
                next_check.isoformat(),
                target_identity,
            ),
        )

    def record_health_batch(
        self,
        values: list[tuple[str, datetime, dict[str, Any]]],
    ) -> None:
        """Persist one bounded health batch atomically with one remote sync."""
        if not values:
            return
        with self.repository.connect() as connection:
            _begin_write(connection)
            for target_identity, checked_at, result in values:
                self._record_health_one(
                    connection,
                    target_identity=target_identity,
                    checked_at=checked_at,
                    result=result,
                )

    def record_health(
        self,
        *,
        target_identity: str,
        checked_at: datetime,
        result: dict[str, Any],
    ) -> None:
        self.record_health_batch([(target_identity, checked_at, result)])

    def admitted_snapshot(
        self,
        *,
        now: datetime,
        max_health_age_hours: int = 48,
    ) -> tuple[list[ProductionTarget], list[dict[str, Any]]]:
        if max_health_age_hours < 1:
            raise ValueError("admission health age must be positive")
        cutoff = (_aware(now) - timedelta(hours=max_health_age_hours)).isoformat()
        with self.repository.connect() as connection:
            rows = connection.execute(
                "SELECT target_identity,source,coordinates_json,company_hint,"
                "health_current_postings,health_inventory_exact,"
                "latest_health_checked_at,latest_health_evidence_sha256 "
                "FROM source_discovery_targets "
                "WHERE admitted=1 AND latest_health_classification='active' "
                "AND latest_health_checked_at>=? ORDER BY target_identity",
                (cutoff,),
            ).fetchall()
        targets = [
            ProductionTarget(
                target_identity=row["target_identity"],
                source=row["source"],
                coordinates=json.loads(row["coordinates_json"]),
                company_hint=row["company_hint"],
                health_current_postings=row["health_current_postings"],
                health_inventory_exact=(
                    None
                    if row["health_inventory_exact"] is None
                    else bool(row["health_inventory_exact"])
                ),
            )
            for row in rows
        ]
        evidence = [
            {
                "target_identity": row["target_identity"],
                "health_checked_at": row["latest_health_checked_at"],
                "health_evidence_sha256": row["latest_health_evidence_sha256"],
            }
            for row in rows
        ]
        if any(
            not value["health_evidence_sha256"]
            for value in evidence
        ):
            raise ValueError("admitted target is missing authoritative health evidence")
        return targets, evidence

    def admitted_targets(
        self,
        *,
        now: datetime,
        max_health_age_hours: int = 48,
    ) -> list[ProductionTarget]:
        targets, _evidence = self.admitted_snapshot(
            now=now,
            max_health_age_hours=max_health_age_hours,
        )
        return targets

    def summary(self, *, now: datetime, max_health_age_hours: int = 48) -> dict[str, Any]:
        admitted = self.admitted_targets(
            now=now,
            max_health_age_hours=max_health_age_hours,
        )
        current = _aware(now).isoformat()
        with self.repository.connect() as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM source_discovery_targets"
            ).fetchone()[0]
            evidence = connection.execute(
                "SELECT COUNT(*) FROM source_discovery_evidence"
            ).fetchone()[0]
            health = connection.execute(
                "SELECT COUNT(*) FROM source_discovery_health_evidence"
            ).fetchone()[0]
            classifications = {
                row["latest_health_classification"]: row["count"]
                for row in connection.execute(
                    "SELECT latest_health_classification,COUNT(*) AS count "
                    "FROM source_discovery_targets "
                    "WHERE latest_health_classification IS NOT NULL "
                    "GROUP BY latest_health_classification"
                ).fetchall()
            }
            unchecked = connection.execute(
                "SELECT COUNT(*) FROM source_discovery_targets "
                "WHERE latest_health_checked_at IS NULL"
            ).fetchone()[0]
            due = connection.execute(
                "SELECT COUNT(*) AS total,"
                "SUM(CASE WHEN latest_health_checked_at IS NULL THEN 1 ELSE 0 END) "
                "AS unchecked,"
                "SUM(CASE WHEN admitted=1 OR admitted_at IS NOT NULL THEN 1 ELSE 0 END) "
                "AS previously_admitted "
                "FROM source_discovery_targets "
                "WHERE next_health_check_at IS NULL OR next_health_check_at<=?",
                (current,),
            ).fetchone()
        return {
            "discovered_targets": total,
            "discovery_evidence_rows": evidence,
            "health_evidence_rows": health,
            "latest_health_classifications": classifications,
            "health_unchecked_targets": unchecked,
            "health_due_targets": int(due["total"] or 0),
            "health_due_unchecked_targets": int(due["unchecked"] or 0),
            "health_due_previously_admitted_targets": int(
                due["previously_admitted"] or 0
            ),
            "runtime_admitted_targets": len(admitted),
        }
