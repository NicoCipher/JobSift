from __future__ import annotations

import re

from job_scout.domain.models import CandidateProfile, Job, JobMatch, MatchDecision, Seniority
from job_scout.normalization.core import normalize_title

MATCHER_VERSION = "deterministic-v1"
SENIORITY_PATTERNS = {
    level: re.compile(rf"\b{re.escape(level.value)}\b", re.IGNORECASE)
    for level in Seniority
    if level is not Seniority.UNKNOWN
}


def _contains(needle: str, haystack: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(needle.casefold())}(?!\w)", haystack.casefold()))


def detect_seniority(job: Job) -> Seniority:
    for text in (job.title, job.description_text or ""):
        for seniority, pattern in SENIORITY_PATTERNS.items():
            if pattern.search(text):
                return seniority
    return Seniority.UNKNOWN


def match_job(job: Job, profile: CandidateProfile) -> JobMatch:
    rejects: list[str] = []
    reasons: list[str] = []
    title = normalize_title(job.title)
    body = f"{job.title}\n{job.description_text or ''}"
    if (
        profile.countries
        and job.country
        and job.country.casefold() not in {c.casefold() for c in profile.countries}
    ):
        rejects.append(f"country {job.country!r} is not allowed")
    if job.remote_status in profile.remote_policy.exclude:
        rejects.append(f"remote status {job.remote_status.value!r} is excluded")
    if (
        profile.remote_policy.allowed
        and job.remote_status not in profile.remote_policy.allowed
        and (job.remote_status.value != "unknown" or profile.remote_policy.reject_unknown)
    ):
        rejects.append(f"remote status {job.remote_status.value!r} is not allowed")
    if (
        profile.employment_types
        and job.employment_type
        and job.employment_type not in profile.employment_types
    ):
        rejects.append(f"employment type {job.employment_type.value!r} is not allowed")
    seniority = detect_seniority(job)
    if seniority in profile.excluded_seniority:
        rejects.append(f"seniority {seniority.value!r} is excluded")
    for word in profile.excluded_keywords:
        if _contains(word, body):
            rejects.append(f"excluded keyword present: {word}")
    for role in profile.target_roles.exclude:
        if normalize_title(role) in title:
            rejects.append(f"excluded role: {role}")
    if rejects:
        return JobMatch(
            job_id=job.id,
            client_id=profile.client_id,
            decision=MatchDecision.REJECT,
            rejection_reasons=rejects,
            matcher_version=MATCHER_VERSION,
        )

    role_hits = [role for role in profile.target_roles.include if normalize_title(role) in title]
    if role_hits:
        reasons.append(f"target role matched: {role_hits[0]}")
    required_missing = [skill for skill in profile.skills.required if not _contains(skill, body)]
    if required_missing:
        return JobMatch(
            job_id=job.id,
            client_id=profile.client_id,
            decision=MatchDecision.REJECT,
            rejection_reasons=[f"required skill missing: {s}" for s in required_missing],
            matcher_version=MATCHER_VERSION,
        )
    skill_hits = [skill for skill in profile.skills.preferred if _contains(skill, body)]
    reasons.extend(f"preferred skill matched: {skill}" for skill in skill_hits)
    score = (3 if role_hits else 0) + min(len(skill_hits), 3)
    if role_hits and skill_hits:
        decision = MatchDecision.STRONG_MATCH
    elif role_hits:
        decision = MatchDecision.POSSIBLE_MATCH
    elif job.description_text is None:
        decision = MatchDecision.NEEDS_REVIEW
    else:
        decision = MatchDecision.REJECT
        rejects.append("title does not match a configured target role")
    return JobMatch(
        job_id=job.id,
        client_id=profile.client_id,
        decision=decision,
        score=score,
        matched_reasons=reasons,
        rejection_reasons=rejects,
        matcher_version=MATCHER_VERSION,
    )
