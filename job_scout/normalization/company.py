from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from job_scout.domain.models import Job


def normalize_employer_name(value: str) -> str:
    """Conservative fallback identity for legacy jobs without an explicit employer_id."""
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    return re.sub(r"\s+", " ", normalized)


def employer_key(job: Job) -> str:
    if job.employer_id:
        return f"id:{job.employer_id}"
    return f"name:{normalize_employer_name(job.company)}"
