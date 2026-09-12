"""Additive, append-only operator state; existing surfacing facts stay authoritative."""

import hashlib
import json
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import uuid4

from job_scout.domain.operator_state import (
    OperatorOutcomeCommand,
    OperatorOutcomeEvent,
    OperatorOutcomeImportReceipt,
    OperatorOutcomeImportRecord,
    OperatorOutcomeProjection,
    OperatorOutcomeResult,
    OperatorOutcomeSubject,
    OutcomeConflict,
    SurfacingEvidence,
)
from job_scout.history import source_identity
from job_scout.normalization.core import canonicalize_url
from job_scout.storage.sqlite import SQLiteRepository

SCHEMA = """
CREATE TABLE IF NOT EXISTS operator_outcome_events (
 event_id TEXT PRIMARY KEY, client_id TEXT NOT NULL,
 subject_type TEXT NOT NULL CHECK(subject_type IN ('posting','history_entry')),
 subject_id TEXT NOT NULL, value TEXT NOT NULL CHECK(value IN ('applied','not_applied')),
 version INTEGER NOT NULL CHECK(version > 0), actor_type TEXT NOT NULL, actor_id TEXT NOT NULL,
 recorded_at TEXT NOT NULL, action TEXT NOT NULL, idempotency_key TEXT NOT NULL,
 request_sha256 TEXT NOT NULL, source_reference TEXT NOT NULL, result_json TEXT NOT NULL,
 UNIQUE(client_id,subject_type,subject_id,version),
 UNIQUE(actor_type,actor_id,client_id,action,idempotency_key)
);
CREATE TABLE IF NOT EXISTS operator_outcome_imports (
 import_record_id TEXT PRIMARY KEY, actor_type TEXT NOT NULL, actor_id TEXT NOT NULL,
 client_id TEXT NOT NULL, source_reference TEXT NOT NULL, external_record_id TEXT NOT NULL,
 request_sha256 TEXT NOT NULL, record_json TEXT NOT NULL, recorded_at TEXT NOT NULL,
 event_id TEXT REFERENCES operator_outcome_events(event_id), result_json TEXT NOT NULL,
 UNIQUE(actor_type,actor_id,client_id,source_reference,external_record_id)
);
CREATE TRIGGER IF NOT EXISTS outcome_events_no_update BEFORE UPDATE ON operator_outcome_events
 BEGIN SELECT RAISE(ABORT,'outcome events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS outcome_events_no_delete BEFORE DELETE ON operator_outcome_events
 BEGIN SELECT RAISE(ABORT,'outcome events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS outcome_imports_no_update BEFORE UPDATE ON operator_outcome_imports
 BEGIN SELECT RAISE(ABORT,'outcome imports are immutable'); END;
CREATE TRIGGER IF NOT EXISTS outcome_imports_no_delete BEFORE DELETE ON operator_outcome_imports
 BEGIN SELECT RAISE(ABORT,'outcome imports are immutable'); END;
"""


def _canonical(model):
    return json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def _digest(model):
    return hashlib.sha256(_canonical(model).encode()).hexdigest()


class OperatorStateStore:
    def __init__(self, repository: SQLiteRepository):
        self.repository = repository
        with repository.connect() as c:
            c.executescript(SCHEMA)

    def _baseline(self, c, client_id, subject):
        if subject.type == "posting":
            if not c.execute("SELECT 1 FROM jobs WHERE id=?", (subject.id,)).fetchone():
                raise OutcomeConflict("subject not found")
            return "unknown", "unknown", None
        row = c.execute(
            "SELECT operator_status,import_id FROM historical_job_links WHERE id=? AND client_id=?",
            (subject.id, client_id),
        ).fetchone()
        if row is None:
            raise OutcomeConflict("subject not found for client")
        return row[0], "historical_import", row[1]

    def _projection(self, c, client_id, subject):
        value, source, import_id = self._baseline(c, client_id, subject)
        row = c.execute(
            "SELECT value,version,event_id FROM operator_outcome_events "
            "WHERE client_id=? AND subject_type=? AND subject_id=? ORDER BY version DESC LIMIT 1",
            (client_id, subject.type, subject.id),
        ).fetchone()
        return OperatorOutcomeProjection(
            client_id=client_id,
            subject=subject,
            value=row[0] if row else value,
            baseline_value=value,
            baseline_source=source,
            baseline_import_id=import_id,
            current_source="explicit_event" if row else source,
            version=row[1] if row else 0,
            latest_event_id=row[2] if row else None,
        )

    def get_projection(self, client_id: str, subject: OperatorOutcomeSubject):
        with self.repository.connect() as c:
            c.execute("BEGIN")
            return self._projection(c, client_id, subject)

    def _surfacing(self, c, client_id, subject):
        self._baseline(c, client_id, subject)
        if subject.type == "history_entry":
            row = c.execute(
                "SELECT import_id,imported_at FROM historical_job_links WHERE id=?", (subject.id,)
            ).fetchone()
            return (
                SurfacingEvidence(kind="historical_import", reference=row[0], recorded_at=row[1]),
            )
        exports = c.execute(
            "SELECT destination,exported_at FROM exports WHERE job_id=? AND client_id=? ORDER BY destination",
            (subject.id, client_id),
        ).fetchall()
        groups = c.execute(
            "SELECT group_id,destination,exported_at FROM group_deliveries WHERE job_id=? AND client_id=? "
            "ORDER BY group_id,destination",
            (subject.id, client_id),
        ).fetchall()
        return tuple(
            [
                SurfacingEvidence(
                    kind="export", reference=subject.id, destination=r[0], recorded_at=r[1]
                )
                for r in exports
            ]
            + [
                SurfacingEvidence(
                    kind="group_delivery", reference=r[0], destination=r[1], recorded_at=r[2]
                )
                for r in groups
            ]
        )

    def get_surfacing_evidence(self, client_id: str, subject: OperatorOutcomeSubject):
        with self.repository.connect() as c:
            c.execute("BEGIN")
            return self._surfacing(c, client_id, subject)

    def list_events(self, client_id: str, subject: OperatorOutcomeSubject):
        with self.repository.connect() as c:
            c.execute("BEGIN")
            self._baseline(c, client_id, subject)
            return tuple(
                OperatorOutcomeResult.model_validate_json(row[0]).event
                for row in c.execute(
                    "SELECT result_json FROM operator_outcome_events WHERE client_id=? AND subject_type=? "
                    "AND subject_id=? ORDER BY version",
                    (client_id, subject.type, subject.id),
                )
            )

    @staticmethod
    def _replay(row, digest):
        if row["request_sha256"] != digest:
            raise OutcomeConflict("idempotency identity reused with incompatible request")
        return OperatorOutcomeResult.model_validate_json(row["result_json"])

    def _append(self, c, command, action, key, digest):
        projection = self._projection(c, command.client_id, command.subject)
        if not self._surfacing(c, command.client_id, command.subject):
            raise OutcomeConflict("exact posting was not surfaced for this client")
        if projection.version != command.expected_version:
            raise OutcomeConflict("stale expected_version")
        event = OperatorOutcomeEvent(
            event_id=str(uuid4()),
            client_id=command.client_id,
            subject=command.subject,
            value=command.value,
            version=projection.version + 1,
            previous_value=projection.value,
            previous_version=projection.version,
            actor_type=command.actor_type,
            actor_id=command.actor_id,
            recorded_at=datetime.now(UTC),
            action=action,
            idempotency_key=key,
            request_sha256=digest,
            source_reference=command.source_reference,
        )
        result = OperatorOutcomeResult(
            event=event,
            projection=projection.model_copy(
                update={
                    "value": event.value,
                    "version": event.version,
                    "latest_event_id": event.event_id,
                    "current_source": "explicit_event",
                }
            ),
        )
        c.execute(
            "INSERT INTO operator_outcome_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event.event_id,
                event.client_id,
                event.subject.type,
                event.subject.id,
                event.value,
                event.version,
                event.actor_type,
                event.actor_id,
                event.recorded_at.isoformat(),
                action,
                key,
                digest,
                event.source_reference,
                result.model_dump_json(),
            ),
        )
        return result

    def record_outcome(self, command: OperatorOutcomeCommand):
        with self.repository.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            digest = _digest(command)
            row = c.execute(
                "SELECT request_sha256,result_json FROM operator_outcome_events WHERE actor_type=? "
                "AND actor_id=? AND client_id=? AND action='record_outcome' AND idempotency_key=?",
                (command.actor_type, command.actor_id, command.client_id, command.idempotency_key),
            ).fetchone()
            if row:
                return self._replay(row, digest)
            return self._append(c, command, "record_outcome", command.idempotency_key, digest)

    def _resolve_import(self, c, record):
        identity = (record.source, record.source_board_id, record.source_job_id)
        url = None
        if record.vacancy_url is not None:
            parsed = urlsplit(record.vacancy_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.path in {"", "/"}
            ):
                raise OutcomeConflict("explicit vacancy URL required")
            url = canonicalize_url(record.vacancy_url)
            if not all(identity):
                identity = source_identity(url)
        # Subject type is explicit: never transfer outcomes between history and postings.
        table = "jobs" if record.subject_type == "posting" else "historical_job_links"
        scope, params = ("", ()) if table == "jobs" else (" AND client_id=?", (record.client_id,))
        rows = []
        if all(identity):
            rows = c.execute(
                f"SELECT id FROM {table} WHERE source=? AND source_board_id=? AND source_job_id=?"
                + scope,
                (*identity, *params),
            ).fetchall()
        if not rows and url:
            column = "canonical_url" if table == "jobs" else "normalized_url"
            rows = c.execute(
                f"SELECT id FROM {table} WHERE {column}=?" + scope, (url, *params)
            ).fetchall()
        if len(rows) != 1:
            raise OutcomeConflict("missing or ambiguous import identity")
        return OperatorOutcomeSubject(type=record.subject_type, id=str(rows[0][0]))

    def import_outcome(self, record: OperatorOutcomeImportRecord):
        """One immutable external observation; unknown never appends an outcome event."""
        with self.repository.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            digest = _digest(record)
            row = c.execute(
                "SELECT request_sha256,result_json FROM operator_outcome_imports WHERE actor_type=? "
                "AND actor_id=? AND client_id=? AND source_reference=? AND external_record_id=?",
                (
                    record.actor_type,
                    record.actor_id,
                    record.client_id,
                    record.source_reference,
                    record.external_record_id,
                ),
            ).fetchone()
            if row:
                return self._replay(row, digest)
            subject = self._resolve_import(c, record)
            if record.observed_outcome == "unknown":
                projection = self._projection(c, record.client_id, subject)
                if not self._surfacing(c, record.client_id, subject):
                    raise OutcomeConflict("exact posting was not surfaced for this client")
                if projection.version != record.expected_version:
                    raise OutcomeConflict("stale expected_version")
                result = OperatorOutcomeResult(event=None, projection=projection)
            else:
                key = json.dumps(
                    [record.source_reference, record.external_record_id], separators=(",", ":")
                )
                command = OperatorOutcomeCommand(
                    client_id=record.client_id,
                    subject=subject,
                    value=record.observed_outcome,
                    expected_version=record.expected_version,
                    actor_type=record.actor_type,
                    actor_id=record.actor_id,
                    idempotency_key=digest,
                    source_reference=record.source_reference,
                )
                result = self._append(c, command, "import_outcome", key, digest)
            c.execute(
                "INSERT INTO operator_outcome_imports VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid4()),
                    record.actor_type,
                    record.actor_id,
                    record.client_id,
                    record.source_reference,
                    record.external_record_id,
                    digest,
                    _canonical(record),
                    datetime.now(UTC).isoformat(),
                    result.event.event_id if result.event else None,
                    result.model_dump_json(),
                ),
            )
            return result

    def get_import_receipt(
        self,
        *,
        actor_type: str,
        actor_id: str,
        client_id: str,
        source_reference: str,
        external_record_id: str,
    ):
        with self.repository.connect() as c:
            row = c.execute(
                "SELECT * FROM operator_outcome_imports WHERE actor_type=? AND actor_id=? "
                "AND client_id=? AND source_reference=? AND external_record_id=?",
                (actor_type, actor_id, client_id, source_reference, external_record_id),
            ).fetchone()
            if row is None:
                raise OutcomeConflict("import receipt not found")
            return OperatorOutcomeImportReceipt(
                import_record_id=row["import_record_id"],
                record=OperatorOutcomeImportRecord.model_validate_json(row["record_json"]),
                recorded_at=row["recorded_at"],
                result=OperatorOutcomeResult.model_validate_json(row["result_json"]),
            )
