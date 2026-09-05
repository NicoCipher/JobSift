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


def _required_items(value: str) -> list[str]:
    items = _items(value)
    if not items:
        raise ValueError("enter at least one target job title")
    return items


def _required_name(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("enter a client name or identifier")
    return value


def _client_id(value: str) -> str:
    identifier = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    if not identifier:
        raise ValueError("client name must contain letters or numbers")
    return identifier


def canonicalize_country_input(value: str) -> str | None:
    value = value.strip()
    if not value or value.casefold() == "any":
        return None
    key = re.sub(r"[^a-z0-9]+", "", value.casefold())
    aliases = {
        "nigeria": "Nigeria",
        "ng": "Nigeria",
        "nga": "Nigeria",
        "india": "India",
        "uk": "United Kingdom",
        "unitedkingdom": "United Kingdom",
        "canada": "Canada",
        "us": "United States",
        "usa": "United States",
        "unitedstates": "United States",
        "unitedstatesofamerica": "United States",
    }
    if key in aliases:
        return aliases[key]
    raise ValueError(
        "currently choose Nigeria, India, United Kingdom, United States, Canada, or Any"
    )


def _work_mode(choice: str) -> tuple[str, RemotePolicy]:
    mapping = {
        "1": ("Remote only", RemoteStatus.REMOTE),
        "2": ("Hybrid only", RemoteStatus.HYBRID),
        "3": ("On-site only", RemoteStatus.ONSITE),
    }
    choice = choice.strip()
    if choice == "4":
        return "Any", RemotePolicy()
    if choice not in mapping:
        raise ValueError("choose a number from 1 to 4")
    label, selected = mapping[choice]
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
            supported = ", ".join(aliases)
            raise ValueError(f"unsupported seniority {item!r}; use one of: {supported}")
        result.add(aliases[key])
    return result


def _employment(choice: str) -> tuple[str, set[EmploymentType]]:
    mapping = {
        "1": ("Full-time", EmploymentType.FULL_TIME),
        "2": ("Part-time", EmploymentType.PART_TIME),
        "3": ("Contract", EmploymentType.CONTRACT),
        "4": ("Temporary", EmploymentType.TEMPORARY),
        "5": ("Internship", EmploymentType.INTERNSHIP),
        "6": ("Freelance", EmploymentType.FREELANCE),
    }
    choice = choice.strip()
    if choice == "7":
        return "Any", set()
    if choice not in mapping:
        raise ValueError("choose a number from 1 to 7")
    label, selected = mapping[choice]
    return label, {selected}


def _maximum_required_experience(value: str) -> int | None:
    value = value.strip()
    if not value or value.casefold() == "any":
        return None
    if not value.isdecimal():
        raise ValueError("enter a whole number from 0 to 50, or leave blank for Any")
    years = int(value)
    if years > 50:
        raise ValueError("enter a whole number from 0 to 50, or leave blank for Any")
    return years


def _confirmation(value: str) -> bool:
    key = value.strip().casefold()
    if key in {"", "y", "yes"}:
        return True
    if key in {"n", "no"}:
        return False
    raise ValueError("enter Y or N")


def _ask[T](
    *, prompt: str, parser: Callable[[str], T], input_fn: InputFunction, output_fn: OutputFunction
) -> T:
    while True:
        try:
            return parser(input_fn(prompt))
        except ValueError as exc:
            output_fn(f"Please try again: {exc}")


def create_profile_interactively(
    *,
    input_fn: InputFunction = input,
    output_fn: OutputFunction = print,
    output_dir: str | Path = "config/clients",
) -> Path | None:
    name = _ask(
        prompt="Client name or identifier: ",
        parser=_required_name,
        input_fn=input_fn,
        output_fn=output_fn,
    )
    roles = _ask(
        prompt="Target job titles (comma-separated): ",
        parser=_required_items,
        input_fn=input_fn,
        output_fn=output_fn,
    )
    country = _ask(
        prompt="Country [Nigeria/India/United Kingdom/United States/Canada/Any]: ",
        parser=canonicalize_country_input,
        input_fn=input_fn,
        output_fn=output_fn,
    )

    output_fn("\nWork mode:\n1. Remote\n2. Hybrid\n3. On-site\n4. Any")
    work_label, remote_policy = _ask(
        prompt="Choose [1-4]: ", parser=_work_mode, input_fn=input_fn, output_fn=output_fn
    )
    excluded_seniority = _ask(
        prompt="Seniority levels to exclude (comma-separated, blank for none): ",
        parser=_seniorities,
        input_fn=input_fn,
        output_fn=output_fn,
    )
    required_skills = _items(input_fn("Required skills (comma-separated, blank for none): "))
    preferred_skills = _items(input_fn("Preferred skills (comma-separated, blank for none): "))
    excluded_titles = _items(input_fn("Titles to exclude (comma-separated, blank for none): "))
    excluded_keywords = _items(input_fn("Keywords to exclude (comma-separated, blank for none): "))

    output_fn(
        "\nEmployment type:\n1. Full-time\n2. Part-time\n3. Contract\n4. Temporary"
        "\n5. Internship\n6. Freelance\n7. Any"
    )
    employment_label, employment_types = _ask(
        prompt="Choose [1-7]: ", parser=_employment, input_fn=input_fn, output_fn=output_fn
    )
    max_required_experience_years = _ask(
        prompt="Maximum required experience to consider (years, blank for Any): ",
        parser=_maximum_required_experience,
        input_fn=input_fn,
        output_fn=output_fn,
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
        max_required_experience_years=max_required_experience_years,
        excluded_seniority=excluded_seniority,
        excluded_keywords=excluded_keywords,
        notes=notes,
    )

    output_fn("\nProfile summary")
    output_fn(f"Client: {name}")
    output_fn(f"Roles: {', '.join(profile.target_roles.include)}")
    output_fn(f"Country: {country or 'Any'}")
    output_fn(f"Work mode: {work_label}")
    output_fn(f"Required skills: {', '.join(required_skills) or 'None'}")
    output_fn(f"Preferred skills: {', '.join(preferred_skills) or 'None'}")
    output_fn(f"Excluded titles: {', '.join(excluded_titles) or 'None'}")
    output_fn(
        "Excluded seniority: "
        + (", ".join(sorted(level.value.title() for level in excluded_seniority)) or "None")
    )
    output_fn(f"Employment type: {employment_label}")
    experience_label = (
        "Any"
        if max_required_experience_years is None
        else f"{max_required_experience_years} year"
        + ("s" if max_required_experience_years != 1 else "")
    )
    output_fn(f"Maximum required experience: {experience_label}")
    output_fn(f"Keywords excluded: {', '.join(excluded_keywords) or 'None'}")
    output_fn(f"Notes: {notes or 'None'}")

    should_save = _ask(
        prompt="\nSave this profile? [Y/n]: ",
        parser=_confirmation,
        input_fn=input_fn,
        output_fn=output_fn,
    )
    if not should_save:
        output_fn("Profile not saved. Run 'python -m job_scout profile create' to restart.")
        return None

    destination = Path(output_dir) / f"{profile.client_id}.json"
    if destination.exists():
        output_fn(f"Profile not saved: {destination} already exists.")
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(profile.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    output_fn(f"\nProfile saved: {destination}")
    output_fn(
        "\nNext:\n"
        f"python -m job_scout collect \\\n  --client {destination} \\\n"
        '  --board <greenhouse-board-token> \\\n  --company "<company-name>" \\\n'
        f"  --csv exports/{profile.client_id}.csv"
    )
    return destination
