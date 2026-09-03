from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

from job_scout.domain.models import (
    CandidateProfile,
    EmploymentType,
    RemotePolicy,
    RemoteStatus,
    RoleTargets,
    Seniority,
    Skills,
    UnknownEligibilityPolicy,
)

InputFunction = Callable[[str], str]
OutputFunction = Callable[[str], None]


def _items(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _client_id(value: str) -> str:
    identifier = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    if not identifier:
        raise ValueError("client name or identifier is required")
    return identifier


def _work_mode(value: str) -> tuple[str, RemotePolicy]:
    key = value.casefold().strip().replace("-", " ")
    if key in {"", "any"}:
        return "Any", RemotePolicy()
    mapping = {
        "remote": ("Remote only", RemoteStatus.REMOTE),
        "hybrid": ("Hybrid only", RemoteStatus.HYBRID),
        "on site": ("On-site only", RemoteStatus.ONSITE),
        "onsite": ("On-site only", RemoteStatus.ONSITE),
    }
    if key not in mapping:
        raise ValueError("work mode must be Remote, Hybrid, On-site, or Any")
    label, selected = mapping[key]
    return label, RemotePolicy(
        allowed={selected},
        exclude={
            status for status in RemoteStatus if status not in {selected, RemoteStatus.UNKNOWN}
        },
        unknown_policy=UnknownEligibilityPolicy.REVIEW,
    )


def _seniorities(value: str) -> set[Seniority]:
    result: set[Seniority] = set()
    aliases = {level.value: level for level in Seniority if level is not Seniority.UNKNOWN}
    for item in _items(value):
        key = item.casefold().strip()
        if key not in aliases:
            raise ValueError(f"unsupported seniority exclusion: {item}")
        result.add(aliases[key])
    return result


def _employment(value: str) -> tuple[str, set[EmploymentType]]:
    key = value.casefold().strip().replace("-", " ").replace("_", " ")
    if key in {"", "any"}:
        return "Any", set()
    mapping = {
        "full time": EmploymentType.FULL_TIME,
        "part time": EmploymentType.PART_TIME,
        "contract": EmploymentType.CONTRACT,
        "temporary": EmploymentType.TEMPORARY,
        "internship": EmploymentType.INTERNSHIP,
        "freelance": EmploymentType.FREELANCE,
    }
    if key not in mapping:
        raise ValueError("unsupported employment type")
    return mapping[key].value.replace("_", " ").title(), {mapping[key]}


def create_profile_interactively(
    *,
    input_fn: InputFunction = input,
    output_fn: OutputFunction = print,
    output_dir: str | Path = "config/clients",
) -> Path:
    name = input_fn("Client name or identifier: ").strip()
    roles = _items(input_fn("Target job titles (comma-separated): "))
    country = input_fn("Country/location requirement (blank for any): ").strip()
    work_label, remote_policy = _work_mode(input_fn("Work mode [Remote/Hybrid/On-site/Any]: "))
    excluded_seniority = _seniorities(
        input_fn("Seniority levels to exclude (comma-separated, blank for none): ")
    )
    required_skills = _items(input_fn("Required skills (comma-separated, blank for none): "))
    preferred_skills = _items(input_fn("Preferred skills (comma-separated, blank for none): "))
    excluded_titles = _items(input_fn("Titles to exclude (comma-separated, blank for none): "))
    excluded_keywords = _items(input_fn("Keywords to exclude (comma-separated, blank for none): "))
    employment_label, employment_types = _employment(
        input_fn(
            "Employment type [Full-time/Part-time/Contract/Temporary/Internship/Freelance/Any]: "
        )
    )
    notes = input_fn("Additional notes (blank for none): ").strip() or None

    profile = CandidateProfile(
        client_id=_client_id(name),
        target_roles=RoleTargets(include=roles, exclude=excluded_titles),
        countries={country} if country else set(),
        unknown_country_policy=UnknownEligibilityPolicy.REVIEW,
        remote_policy=remote_policy,
        skills=Skills(required=required_skills, preferred=preferred_skills),
        employment_types=employment_types,
        unknown_employment_type_policy=UnknownEligibilityPolicy.REVIEW,
        excluded_seniority=excluded_seniority,
        excluded_keywords=excluded_keywords,
        notes=notes,
    )

    output_fn("\nProfile summary")
    output_fn(f"Client: {name} ({profile.client_id})")
    output_fn(f"Roles: {', '.join(profile.target_roles.include)}")
    output_fn(f"Country: {country or 'Any'}")
    output_fn(f"Work mode: {work_label}")
    output_fn(f"Required skills: {', '.join(required_skills) or 'None'}")
    output_fn(f"Preferred skills: {', '.join(preferred_skills) or 'None'}")
    output_fn(
        "Excluded seniority: "
        + (", ".join(sorted(level.value.title() for level in excluded_seniority)) or "None")
    )
    output_fn(f"Employment type: {employment_label}")

    destination = Path(output_dir) / f"{profile.client_id}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"profile already exists: {destination}")
    destination.write_text(
        json.dumps(profile.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    output_fn(f"Saved: {destination}")
    return destination
