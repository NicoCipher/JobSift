from __future__ import annotations

import re

from job_scout.domain.models import (
    CandidateProfile,
    EmploymentTypeRule,
    Job,
    JobMatch,
    MatchDecision,
    RemoteStatus,
    RuleIntent,
    SearchBrief,
    Seniority,
    TargetMarketRule,
    UnknownEligibilityPolicy,
    WorkEligibilityRule,
    WorkModeRule,
)
from job_scout.normalization.core import normalize_title

from .experience import extract_required_experience_years

MATCHER_VERSION = "deterministic-v5"

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

US_WORK_ELIGIBILITY_PATTERNS = (
    re.compile(
        r"\b(?:must\s+be\s+(?:a\s+)?)?(?:United States|U\.S\.)\s+Citizen\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:United States|U\.S\.)\s+citizenship\s+(?:is\s+)?required\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bauthorized to work in (?:the )?United States\b", re.IGNORECASE),
)

MANAGEMENT_TITLE_PATTERNS = (
    ("manager", re.compile(r"\bmanager\b", re.IGNORECASE)),
    ("director", re.compile(r"\bdirector\b", re.IGNORECASE)),
    ("head", re.compile(r"\bhead\s+of\b", re.IGNORECASE)),
    (
        "vice president",
        re.compile(r"\b(?:vice\s+president|[es]?vp)\b", re.IGNORECASE),
    ),
    (
        "chief",
        re.compile(
            r"\b(?:chief\b|ceo\b|cto\b|cio\b|ciso\b|coo\b|cfo\b|cmo\b|cro\b|cpo\b|chro\b)",
            re.IGNORECASE,
        ),
    ),
)


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


def _restricted_work_countries(description: str) -> set[str]:
    if any(pattern.search(description) for pattern in US_WORK_ELIGIBILITY_PATTERNS):
        return {"United States"}
    return set()


def search_brief_from_candidate_profile(profile: CandidateProfile) -> SearchBrief:
    if profile.remote_policy.allowed:
        work_mode = WorkModeRule(
            modes=profile.remote_policy.allowed,
            intent=RuleIntent.MUST,
            unknown_policy=profile.remote_policy.unknown_policy,
        )
    elif profile.remote_policy.exclude:
        work_mode = WorkModeRule(
            modes=profile.remote_policy.exclude,
            intent=RuleIntent.AVOID,
            unknown_policy=profile.remote_policy.unknown_policy,
        )
    else:
        work_mode = WorkModeRule()

    return SearchBrief(
        client_id=profile.client_id,
        target_roles=profile.target_roles.include,
        target_market=TargetMarketRule(),
        work_mode=work_mode,
        management_roles=RuleIntent.IGNORE,
        excluded_titles=profile.target_roles.exclude,
        excluded_seniority=profile.excluded_seniority,
        must_have_terms=profile.skills.required,
        preferred_terms=profile.skills.preferred,
        avoid_terms=profile.excluded_keywords,
        employment_type=EmploymentTypeRule(
            types=profile.employment_types,
            intent=RuleIntent.MUST if profile.employment_types else RuleIntent.IGNORE,
            unknown_policy=profile.unknown_employment_type_policy,
        ),
        max_required_experience_years=profile.max_required_experience_years,
        work_eligibility=WorkEligibilityRule(
            countries=profile.countries,
            intent=RuleIntent.MUST if profile.countries else RuleIntent.IGNORE,
            unknown_policy=profile.unknown_country_policy,
        ),
        notes=profile.notes,
    )


def _job_countries(job: Job) -> set[str]:
    countries = set(job.eligible_countries)
    if not countries and job.country:
        countries.add(job.country)
    return countries


def _country_rule(
    *,
    label: str,
    configured: set[str],
    observed: set[str],
    intent: RuleIntent,
    unknown_policy: UnknownEligibilityPolicy,
    rejects: list[str],
    reviews: list[str],
    reasons: list[str],
) -> int:
    if intent is RuleIntent.IGNORE or not configured:
        return 0
    observed_by_key = {country.casefold(): country for country in observed}
    configured_by_key = {country.casefold(): country for country in configured}
    overlap_keys = set(observed_by_key) & set(configured_by_key)
    overlap = {configured_by_key[key] for key in overlap_keys}
    if intent is RuleIntent.MUST:
        if not observed:
            _unknown_evidence(
                label=label,
                policy=unknown_policy,
                rejects=rejects,
                reviews=reviews,
            )
        elif not overlap:
            countries = ", ".join(sorted(observed))
            rejects.append(f"{label} countries [{countries}] do not match the brief")
        else:
            reasons.append(f"{label} matched: {', '.join(sorted(overlap))}")
    elif intent is RuleIntent.AVOID and overlap:
        rejects.append(f"{label} includes avoided country: {', '.join(sorted(overlap))}")
    elif intent is RuleIntent.PREFER and overlap:
        reasons.append(f"preferred {label} matched: {', '.join(sorted(overlap))}")
        return 1
    return 0


def _management_title_kind(title: str) -> str | None:
    for label, pattern in MANAGEMENT_TITLE_PATTERNS:
        if pattern.search(title):
            return label
    return None


def match_job(job: Job, profile: SearchBrief | CandidateProfile) -> JobMatch:
    brief = (
        search_brief_from_candidate_profile(profile)
        if isinstance(profile, CandidateProfile)
        else profile
    )
    rejects: list[str] = []
    reviews: list[str] = []
    reasons: list[str] = []
    preference_hits = 0
    title = normalize_title(job.title)
    body = f"{job.title}\n{job.description_text or ''}"

    role_hits = [role for role in brief.target_roles if normalize_title(role) in title]
    if not role_hits:
        return JobMatch(
            job_id=job.id,
            client_id=brief.client_id,
            decision=MatchDecision.REJECT,
            rejection_reasons=["title does not match a configured target role"],
            matcher_version=MATCHER_VERSION,
        )
    reasons.append(f"target role matched: {role_hits[0]}")

    management_kind = _management_title_kind(job.title)
    if brief.management_roles is RuleIntent.AVOID and management_kind:
        rejects.append(f"management/executive title excluded: {management_kind}")
    elif brief.management_roles is RuleIntent.MUST and not management_kind:
        rejects.append("management/executive title is required by the brief")
    elif brief.management_roles is RuleIntent.PREFER and management_kind:
        reasons.append(f"preferred management/executive title matched: {management_kind}")
        preference_hits += 1

    for role in brief.excluded_titles:
        if normalize_title(role) in title:
            rejects.append(f"excluded role: {role}")

    seniority = detect_seniority(job)
    excluded_title_levels = [
        level for level in title_seniorities(job) if level in brief.excluded_seniority
    ]
    if excluded_title_levels:
        rejects.append(f"seniority {excluded_title_levels[0].value!r} is excluded")
    elif seniority in brief.excluded_seniority:
        rejects.append(f"seniority {seniority.value!r} is excluded")

    job_countries = _job_countries(job)
    preference_hits += _country_rule(
        label="target market",
        configured=brief.target_market.countries,
        observed=job_countries,
        intent=brief.target_market.intent,
        unknown_policy=brief.target_market.unknown_policy,
        rejects=rejects,
        reviews=reviews,
        reasons=reasons,
    )

    if brief.work_mode.intent is RuleIntent.MUST:
        if job.remote_status is RemoteStatus.UNKNOWN:
            _unknown_evidence(
                label="remote status",
                policy=brief.work_mode.unknown_policy,
                rejects=rejects,
                reviews=reviews,
            )
        elif job.remote_status not in brief.work_mode.modes:
            rejects.append(f"work mode {job.remote_status.value!r} does not match the brief")
        else:
            reasons.append(f"work mode matched: {job.remote_status.value}")
    elif brief.work_mode.intent is RuleIntent.AVOID and job.remote_status in brief.work_mode.modes:
        rejects.append(f"work mode {job.remote_status.value!r} is avoided")
    elif brief.work_mode.intent is RuleIntent.PREFER and job.remote_status in brief.work_mode.modes:
        reasons.append(f"preferred work mode matched: {job.remote_status.value}")
        preference_hits += 1

    preference_hits += _country_rule(
        label="work eligibility",
        configured=brief.work_eligibility.countries,
        observed=job_countries,
        intent=brief.work_eligibility.intent,
        unknown_policy=brief.work_eligibility.unknown_policy,
        rejects=rejects,
        reviews=reviews,
        reasons=reasons,
    )
    if brief.work_eligibility.intent is RuleIntent.MUST:
        restricted_countries = _restricted_work_countries(job.description_text or "")
        allowed = {country.casefold() for country in brief.work_eligibility.countries}
        if (
            restricted_countries
            and not {country.casefold() for country in restricted_countries} & allowed
        ):
            countries = ", ".join(sorted(restricted_countries))
            rejects.append(
                f"explicit citizenship or work authorization restricts the role to {countries}"
            )

    if brief.employment_type.intent is RuleIntent.MUST:
        if job.employment_type is None:
            _unknown_evidence(
                label="employment type",
                policy=brief.employment_type.unknown_policy,
                rejects=rejects,
                reviews=reviews,
            )
        elif job.employment_type not in brief.employment_type.types:
            rejects.append(
                f"employment type {job.employment_type.value!r} does not match the brief"
            )
        else:
            reasons.append(f"employment type matched: {job.employment_type.value}")
    elif (
        brief.employment_type.intent is RuleIntent.AVOID
        and job.employment_type in brief.employment_type.types
    ):
        rejects.append(f"employment type {job.employment_type.value!r} is avoided")
    elif (
        brief.employment_type.intent is RuleIntent.PREFER
        and job.employment_type in brief.employment_type.types
    ):
        reasons.append(f"preferred employment type matched: {job.employment_type.value}")
        preference_hits += 1

    if brief.max_required_experience_years is not None:
        minimum_years = extract_required_experience_years(job.title, job.description_text)
        if minimum_years is not None and minimum_years > brief.max_required_experience_years:
            rejects.append(
                f"minimum required experience {minimum_years} years exceeds configured maximum "
                f"{brief.max_required_experience_years} years"
            )

    for word in brief.avoid_terms:
        if _contains(word, body):
            rejects.append(f"excluded keyword present: {word}")

    missing_must_terms = [word for word in brief.must_have_terms if not _contains(word, body)]
    if missing_must_terms and job.description_text is None:
        reviews.extend(f"must-have term cannot be verified: {word}" for word in missing_must_terms)
    else:
        rejects.extend(f"must-have term missing: {word}" for word in missing_must_terms)
    reasons.extend(
        f"must-have term matched: {word}" for word in brief.must_have_terms if _contains(word, body)
    )

    if rejects:
        return JobMatch(
            job_id=job.id,
            client_id=brief.client_id,
            decision=MatchDecision.REJECT,
            rejection_reasons=rejects,
            matcher_version=MATCHER_VERSION,
        )

    skill_hits = [skill for skill in brief.preferred_terms if _contains(skill, body)]
    reasons.extend(f"preferred skill matched: {skill}" for skill in skill_hits)
    preference_hits += len(skill_hits)
    score = 3 + min(preference_hits, 3)
    if reviews:
        decision = MatchDecision.NEEDS_REVIEW
        reasons.extend(f"needs review: {reason}" for reason in reviews)
    elif preference_hits:
        decision = MatchDecision.STRONG_MATCH
    else:
        decision = MatchDecision.POSSIBLE_MATCH

    return JobMatch(
        job_id=job.id,
        client_id=brief.client_id,
        decision=decision,
        score=score,
        matched_reasons=reasons,
        rejection_reasons=rejects,
        matcher_version=MATCHER_VERSION,
    )
