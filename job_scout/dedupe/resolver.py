import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from job_scout.domain.models import Job, RemoteStatus
from job_scout.normalization.core import canonicalize_url
from job_scout.normalization.location import COUNTRY_PATTERNS

# Persisted evidence keys use this version so later algorithms cannot accidentally
# compare incompatible fingerprints in an existing database.
DEDUPE_VERSION = "dedupe-v1"


def _text(value: str | None) -> str:
    return " ".join(unicodedata.normalize("NFKC", value or "").casefold().split())


def delivery_keys(job: Job) -> set[tuple[str, str]]:
    keys = set()
    for url in (job.canonical_url, job.job_url, job.apply_url):
        if url is None:
            continue
        parts = urlsplit(canonicalize_url(str(url)))
        # A home page or generic careers landing page is not vacancy identity.
        if parts.path.lower() in {"/", "/jobs", "/careers", "/apply"}:
            continue
        normalized = urlunsplit(
            parts._replace(query=urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True))))
        )
        keys.add(("url", normalized))

    description = _text(job.description_text)
    countries = job.eligible_countries or ({job.country} if job.country else set())
    if len(description.split()) < 40 or not countries or job.remote_status is RemoteStatus.UNKNOWN:
        return keys
    # Retain subcountry restrictions and unknown text. Only established country
    # aliases and the already-structured work-mode label are redundant.
    location = job.location_text or ""
    for country, pattern in COUNTRY_PATTERNS.items():
        if country in countries:
            location = pattern.sub(country.casefold(), location)
    location = re.sub(r"\b(?:remote|hybrid|on[- ]?site)\b", "", location, flags=re.IGNORECASE)
    for country in countries:
        location = re.sub(re.escape(country), "", location, flags=re.IGNORECASE)
    location = re.sub(r"[\s,;|/–—-]+", " ", location).strip()
    facts = [
        _text(job.company),
        _text(job.title),
        description,
        sorted(_text(country) for country in countries),
        job.remote_status.value,
        _text(location),
        _text(job.city),
        _text(job.region),
        _text(job.department),
        job.employment_type.value if job.employment_type else None,
    ]
    key = hashlib.sha256(json.dumps(facts, ensure_ascii=False).encode()).hexdigest()
    keys.add((DEDUPE_VERSION, key))
    return keys


def representative_key(job: Job) -> tuple:
    def timestamp(value: datetime | None) -> float:
        if value is None:
            return 0
        return value.replace(tzinfo=value.tzinfo or UTC).timestamp()

    # Candidates are only the matching postings verified in this collection.
    richness = sum(
        bool(value)
        for value in (
            job.eligible_countries,
            job.department,
            job.employment_type,
            job.city,
            job.region,
        )
    )
    return (
        -int(job.apply_url is not None),
        -len(_text(job.description_text)),
        -richness,
        -timestamp(job.updated_at),
        -timestamp(job.posted_at),
        job.source,
        job.source_board_id,
        job.source_job_id,
        job.id,
    )


def identity_key(job: Job) -> tuple[str, ...]:
    if job.source_job_id:
        return ("provider", job.source, job.source_board_id, job.source_job_id)
    return ("url", str(job.canonical_url))


def select_preferred_url(job: Job) -> str:
    return str(job.apply_url or job.canonical_url)
