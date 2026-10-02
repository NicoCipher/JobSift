"""Shared retention-age semantics for active job payloads."""

from __future__ import annotations

from datetime import UTC, datetime


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def retention_basis(
    *,
    retention_posted_at: datetime | None,
    posted_at: datetime | None,
    first_seen_at: datetime,
) -> datetime:
    """Return the timestamp that governs full-payload retention.

    Explicit retention evidence wins, then the payload posting timestamp, then
    first-seen time for postings whose age is otherwise unknown.
    """
    return _aware(retention_posted_at or posted_at or first_seen_at)
