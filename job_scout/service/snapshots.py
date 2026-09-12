"""Bounded immutable projections and opaque traversal handles; process-local, 30 minute TTL."""

import copy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from uuid import UUID, uuid4

from job_scout.service.errors import ServiceError


@dataclass
class Snapshot:
    id: str
    owner: tuple
    route: str
    query: dict
    data: list
    details: dict
    expires: datetime


class Snapshots:
    def __init__(self):
        self.lock = RLock()
        self.items = {}
        self.cursors = {}
        self.clock = lambda: datetime.now(UTC)

    def get(self, id, owner):
        with self.lock:
            snap = self.items.get(id)
            if snap is None or snap.expires <= self.clock():
                raise ServiceError("SNAPSHOT_EXPIRED")
            if snap.owner != owner:
                raise ServiceError("INVALID_CURSOR")
            return copy.deepcopy(snap)

    def create(self, owner, route, query, data, details):
        with self.lock:
            expired = {id for id, s in self.items.items() if s.expires <= self.clock()}
            self.items = {id: s for id, s in self.items.items() if id not in expired}
            # Unknown/removed snapshot IDs still resolve as expired. Cursor handles expire with them.
            self.cursors = {k: v for k, v in self.cursors.items() if v[0] not in expired}
            if sum(s.owner[0] == owner[0] for s in self.items.values()) >= 5:
                raise ServiceError("RATE_LIMITED")
            snap = Snapshot(
                str(uuid4()),
                owner,
                route,
                copy.deepcopy(query),
                copy.deepcopy(data),
                copy.deepcopy(details),
                self.clock() + timedelta(minutes=30),
            )
            self.items[snap.id] = snap
            return copy.deepcopy(snap)

    def cursor(self, snap, position):
        with self.lock:
            key = (snap.id, position)
            # Reuse handles rather than allocate memory on every replay.
            for id, value in self.cursors.items():
                if value == key:
                    return id
            id = str(uuid4())
            self.cursors[id] = key
            return id

    def resolve(self, cursor, owner):
        with self.lock:
            if cursor not in self.cursors:
                try:
                    UUID(cursor)
                except (ValueError, AttributeError):
                    raise ServiceError("INVALID_CURSOR") from None
                raise ServiceError("SNAPSHOT_EXPIRED")
            id, pos = self.cursors[cursor]
            return self.get(id, owner), pos
