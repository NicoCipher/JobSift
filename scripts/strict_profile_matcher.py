"""Profile-scoped qualification rules layered over the frozen generic matcher.

This module is deliberately outside job_scout/: historical matcher and capacity
replay inputs remain byte-for-byte immutable.
"""
from __future__ import annotations

import re

from job_scout.domain.models import Job, JobMatch, MatchDecision, SearchBrief
from job_scout.matching.matcher import match_job as generic_match_job
from job_scout.normalization.location import normalize_location

STRICT_PROFILE_ID = "nicocipher"

_ON_SITE_ONLY = (
    re.compile(r"\bthis\s+(?:role|position)\s+is\s+(?:primarily\s+)?on[- ]site\b", re.I),
    re.compile(r"\bon[- ]site\s+in\s+[A-Z][^.!?\n]{0,100}", re.I),
)
_US_RESIDENCE_ONLY = (
    re.compile(r"\bmust\s+(?:already\s+)?be\s+(?:physically\s+)?based\s+in\s+(?:the\s+)?(?:United States|U\.?S\.?|USA)\b", re.I),
    re.compile(r"\bmust\s+(?:currently\s+)?reside\s+in\s+(?:the\s+)?(?:United States|U\.?S\.?|USA)\b", re.I),
    re.compile(r"\b(?:applicants|candidates)\s+must\s+be\s+(?:located|based)\s+in\s+(?:the\s+)?(?:United States|U\.?S\.?|USA)\b", re.I),
    re.compile(r"\b(?:United States|U\.?S\.?|USA)[- ]based\s+(?:applicants|candidates)\s+only\b", re.I),
)
_TEST_SPECIALIST = re.compile(r"\b(?:software\s+)?engineer\s+in\s+test\b|\bSDET\b", re.I)


def match_profile_job(job: Job, brief: SearchBrief) -> JobMatch:
    """Apply strict software/US-remote rules only to the nicocipher profile."""
    match = generic_match_job(job, brief)
    if brief.client_id != STRICT_PROFILE_ID or match.decision is MatchDecision.REJECT:
        return match

    rejects: list[str] = []
    description = job.description_text or ""

    # Explicit posting-location evidence outranks broad ATS eligibility metadata.
    location_countries = set(normalize_location(job.location_text, job.offices).countries)
    if location_countries and "United States" not in location_countries:
        rejects.append(
            "explicit posting location is outside the United States: "
            + ", ".join(sorted(location_countries))
        )

    if job.remote_status.value == "remote" and any(p.search(description) for p in _ON_SITE_ONLY):
        rejects.append("description explicitly identifies the role as on-site despite remote metadata")

    residence = (brief.candidate_residence or "").casefold()
    if "nigeria" in residence and any(p.search(description) for p in _US_RESIDENCE_ONLY):
        rejects.append("role requires applicants to reside or be based in the United States")

    if rejects:
        return match.model_copy(
            update={
                "decision": MatchDecision.REJECT,
                "score": None,
                "rejection_reasons": [*match.rejection_reasons, *rejects],
            }
        )

    if _TEST_SPECIALIST.search(job.title):
        return match.model_copy(
            update={
                "decision": MatchDecision.NEEDS_REVIEW,
                "matched_reasons": [
                    *match.matched_reasons,
                    "needs review: test/QA-specialist title needs confirmation against the software-development target",
                ],
            }
        )
    return match
