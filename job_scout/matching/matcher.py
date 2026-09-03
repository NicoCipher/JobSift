from __future__ import annotations

import re

from job_scout.domain.models import (
    CandidateProfile,
    Job,
    JobMatch,
    MatchDecision,
    RemoteStatus,
    Seniority,
    UnknownEligibilityPolicy,
)
from job_scout.normalization.core import normalize_title

MATCHER_VERSION = "deterministic-v2"

# Higher-authority title evidence wins when a title contains multiple levels.
SENIORITY_PRECEDENCE = (
    Seniority.DIRECTOR,
    Seniority.MANAGER,
    Seniority.PRINCIPAL,
    Seniority.STAFF,
    Seniority.LEAD,
    Seniority.SENIOR,
    Seniority.MID,
    Seniority.ASSOCIATE,
    Seniority.JUNIOR,
    Seniority.ENTRY,
    Seniority.INTERN,
)
SENIORITY_PATTERNS = {
    level: re.compile(rf"\b{re.escape(level.value)}\b", re.IGNORECASE)
    for level in SENIORITY_PRECEDENCE
}


def _contains(needle: str, haystack: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(needle.casefold())}(?!\w)", haystack.casefold()))


def title_seniorities(job: Job) -> tuple[Seniority, ...]:
    return tuple(
        level for level in SENIORITY_PRECEDENCE if SENIORITY_PATTERNS[level].search(job.title)
    )


def detect_seniority(job: Job) -> Seniority:
    title_levels = title_seniorities(job)
    if title_levels:
        return title_levels[0]

    # Description evidence is accepted only when explicitly labelled. Generic prose
    # such as "work with senior managers" is not evidence of this vacancy's level.
    description = job.description_text or ""
    for seniority in SENIORITY_PRECEDENCE:
        explicit = re.compile(
            rf"\b(?:seniority|role|position)\s+level\s*[:\-]\s*{re.escape(seniority.value)}\b",
            re.IGNORECASE,
        )
        if explicit.search(description):
            return seniority
    return Seniority.UNKNOWN


def _unknown_evidence(
    *, label: str, policy: UnknownEligibilityPolicy, rejects: list[str], reviews: list[str]
) -> None:
    if policy is UnknownEligibilityPolicy.REJECT:
        rejects.append(f"{label} is unknown and profile policy rejects unknown evidence")
    elif policy is UnknownEligibilityPolicy.REVIEW:
        reviews.append(f"{label} is unknown")


def match_job(job: Job, profile: CandidateProfile) -> JobMatch:
    rejects: list[str] = []
    reviews: list[str] = []
    reasons: list[str] = []
    title = normalize_title(job.title)
    body = f"{job.title}\n{job.description_text or ''}"

    if profile.countries:
        if job.country is None:
            _unknown_evidence(
                label="country",
                policy=profile.unknown_country_policy,
                rejects=rejects,
                reviews=reviews,
            )
        elif job.country.casefold() not in {c.casefold() for c in profile.countries}:
            rejects.append(f"country {job.country!r} is not allowed")

    if job.remote_status in profile.remote_policy.exclude:
        rejects.append(f"remote status {job.remote_status.value!r} is excluded")
    if profile.remote_policy.allowed and job.remote_status not in profile.remote_policy.allowed:
        if job.remote_status is RemoteStatus.UNKNOWN:
            _unknown_evidence(
                label="remote status",
                policy=profile.remote_policy.unknown_policy,
                rejects=rejects,
                reviews=reviews,
            )
        else:
            rejects.append(f"remote status {job.remote_status.value!r} is not allowed")

    if profile.employment_types:
        if job.employment_type is None:
            _unknown_evidence(
                label="employment type",
                policy=profile.unknown_employment_type_policy,
                rejects=rejects,
                reviews=reviews,
            )
        elif job.employment_type not in profile.employment_types:
            rejects.append(f"employment type {job.employment_type.value!r} is not allowed")

    seniority = detect_seniority(job)
    excluded_title_levels = [
        level for level in title_seniorities(job) if level in profile.excluded_seniority
    ]
    if excluded_title_levels:
        rejects.append(f"seniority {excluded_title_levels[0].value!r} is excluded")
    elif seniority in profile.excluded_seniority:
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
            rejection_reasons=[f"required skill missing: {skill}" for skill in required_missing],
            matcher_version=MATCHER_VERSION,
        )

    skill_hits = [skill for skill in profile.skills.preferred if _contains(skill, body)]
    reasons.extend(f"preferred skill matched: {skill}" for skill in skill_hits)
    score = (3 if role_hits else 0) + min(len(skill_hits), 3)
    if not role_hits:
        decision = (
            MatchDecision.NEEDS_REVIEW if job.description_text is None else MatchDecision.REJECT
        )
        if decision is MatchDecision.REJECT:
            rejects.append("title does not match a configured target role")
    elif reviews:
        decision = MatchDecision.NEEDS_REVIEW
        reasons.extend(f"needs review: {reason}" for reason in reviews)
    elif skill_hits:
        decision = MatchDecision.STRONG_MATCH
    else:
        decision = MatchDecision.POSSIBLE_MATCH

    return JobMatch(
        job_id=job.id,
        client_id=profile.client_id,
        decision=decision,
        score=score,
        matched_reasons=reasons,
        rejection_reasons=rejects,
        matcher_version=MATCHER_VERSION,
    )
