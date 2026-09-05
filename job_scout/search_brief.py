from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from job_scout.domain.models import (
    CandidateProfile,
    EmploymentTypeRule,
    RuleIntent,
    SearchBrief,
    TargetMarketRule,
    UnknownEligibilityPolicy,
    WorkEligibilityRule,
    WorkModeRule,
)
from job_scout.matching.matcher import search_brief_from_candidate_profile
from job_scout.profile import (
    InputFunction,
    OutputFunction,
    _ask,
    _client_id,
    _confirmation,
    _employment,
    _items,
    _maximum_required_experience,
    _required_items,
    _required_name,
    _work_mode,
    canonicalize_country_input,
)


def _yes_no(value: str, *, default: bool) -> bool:
    key = value.strip().casefold()
    if not key:
        return default
    if key in {"y", "yes"}:
        return True
    if key in {"n", "no"}:
        return False
    raise ValueError("enter Y or N")


def _yes_default(value: str) -> bool:
    return _yes_no(value, default=True)


def _no_default(value: str) -> bool:
    return _yes_no(value, default=False)


def _countries(value: str) -> set[str]:
    raw = _items(value)
    if not raw:
        return set()
    countries = {canonicalize_country_input(item) for item in raw}
    if None in countries and len(countries) > 1:
        raise ValueError("choose specific countries or Any, not both")
    return {country for country in countries if country is not None}


def _required_countries(value: str) -> set[str]:
    countries = _countries(value)
    if not countries:
        raise ValueError("enter at least one allowed work country")
    return countries


def _ask_yes_no(
    *,
    prompt: str,
    default: bool,
    input_fn: InputFunction,
    output_fn: OutputFunction,
) -> bool:
    parser: Callable[[str], bool] = _yes_default if default else _no_default
    return _ask(prompt=prompt, parser=parser, input_fn=input_fn, output_fn=output_fn)


def load_search_brief(path: str | Path) -> SearchBrief:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("brief_version") == "operator-style-sourcing-brief-v1":
        return SearchBrief.model_validate(data)
    return search_brief_from_candidate_profile(CandidateProfile.model_validate(data))


def create_search_brief_interactively(
    *,
    input_fn: InputFunction = input,
    output_fn: OutputFunction = print,
    output_dir: str | Path = "config/search_briefs",
) -> Path | None:
    name = _ask(
        prompt="Client name or identifier: ",
        parser=_required_name,
        input_fn=input_fn,
        output_fn=output_fn,
    )
    roles = _ask(
        prompt="What jobs are you looking for? (comma-separated): ",
        parser=_required_items,
        input_fn=input_fn,
        output_fn=output_fn,
    )
    target_countries = _ask(
        prompt=("Target job market [Nigeria/India/United Kingdom/United States/Canada/Any]: "),
        parser=_countries,
        input_fn=input_fn,
        output_fn=output_fn,
    )
    candidate_residence = (
        input_fn("Candidate residence (optional, informational only): ").strip() or None
    )

    output_fn("\nWork mode:\n1. Remote\n2. Hybrid\n3. On-site\n4. Any")
    work_label, legacy_work_policy = _ask(
        prompt="Choose [1-4]: ", parser=_work_mode, input_fn=input_fn, output_fn=output_fn
    )
    work_mode = WorkModeRule(
        modes=legacy_work_policy.allowed,
        intent=RuleIntent.MUST if legacy_work_policy.allowed else RuleIntent.IGNORE,
        unknown_policy=UnknownEligibilityPolicy.REVIEW,
    )

    avoid_management = _ask_yes_no(
        prompt="Exclude management/executive roles? [Y/n]: ",
        default=True,
        input_fn=input_fn,
        output_fn=output_fn,
    )
    excluded_titles = _items(
        input_fn("Other titles to exclude (comma-separated, blank for none): ")
    )
    must_have_terms = _items(input_fn("Must-have terms (comma-separated, blank for none): "))
    preferred_terms = _items(input_fn("Preferred terms (comma-separated, blank for none): "))
    avoid_terms = _items(input_fn("Terms to avoid (comma-separated, blank for none): "))

    advanced = _ask_yes_no(
        prompt="Configure advanced restrictions? [y/N]: ",
        default=False,
        input_fn=input_fn,
        output_fn=output_fn,
    )
    max_experience = None
    work_eligibility = WorkEligibilityRule()
    employment_label = "Any"
    employment_type = EmploymentTypeRule()
    if advanced:
        max_experience = _ask(
            prompt="Maximum required experience to consider (years, blank for Any): ",
            parser=_maximum_required_experience,
            input_fn=input_fn,
            output_fn=output_fn,
        )
        enforce_eligibility = _ask_yes_no(
            prompt="Require explicit work-country eligibility? [y/N]: ",
            default=False,
            input_fn=input_fn,
            output_fn=output_fn,
        )
        if enforce_eligibility:
            eligibility_countries = _ask(
                prompt="Allowed work countries (comma-separated): ",
                parser=_required_countries,
                input_fn=input_fn,
                output_fn=output_fn,
            )
            work_eligibility = WorkEligibilityRule(
                countries=eligibility_countries,
                intent=RuleIntent.MUST,
                unknown_policy=UnknownEligibilityPolicy.REVIEW,
            )

        output_fn(
            "\nEmployment type:\n1. Full-time\n2. Part-time\n3. Contract\n4. Temporary"
            "\n5. Internship\n6. Freelance\n7. Any"
        )
        employment_label, employment_types = _ask(
            prompt="Choose [1-7]: ",
            parser=_employment,
            input_fn=input_fn,
            output_fn=output_fn,
        )
        employment_type = EmploymentTypeRule(
            types=employment_types,
            intent=RuleIntent.MUST if employment_types else RuleIntent.IGNORE,
            unknown_policy=UnknownEligibilityPolicy.REVIEW,
        )

    notes = input_fn("Additional notes (blank for none): ").strip() or None
    brief = SearchBrief(
        client_id=_client_id(name),
        target_roles=roles,
        target_market=TargetMarketRule(
            countries=target_countries,
            intent=RuleIntent.MUST if target_countries else RuleIntent.IGNORE,
            unknown_policy=UnknownEligibilityPolicy.REVIEW,
        ),
        work_mode=work_mode,
        management_roles=RuleIntent.AVOID if avoid_management else RuleIntent.IGNORE,
        excluded_titles=excluded_titles,
        must_have_terms=must_have_terms,
        preferred_terms=preferred_terms,
        avoid_terms=avoid_terms,
        employment_type=employment_type,
        max_required_experience_years=max_experience,
        candidate_residence=candidate_residence,
        work_eligibility=work_eligibility,
        notes=notes,
    )

    output_fn("\nSearch brief summary")
    output_fn(f"Client: {name}")
    output_fn(f"Jobs: {', '.join(brief.target_roles)}")
    output_fn(f"Target market: {', '.join(sorted(target_countries)) or 'Any'}")
    output_fn(f"Candidate residence: {candidate_residence or 'Not provided'}")
    output_fn(f"Work mode: {work_label}")
    output_fn(f"Exclude management/executive roles: {'Yes' if avoid_management else 'No'}")
    output_fn(f"Other excluded titles: {', '.join(excluded_titles) or 'None'}")
    output_fn(f"Must-have terms: {', '.join(must_have_terms) or 'None'}")
    output_fn(f"Preferred terms: {', '.join(preferred_terms) or 'None'}")
    output_fn(f"Terms to avoid: {', '.join(avoid_terms) or 'None'}")
    output_fn(
        "Maximum required experience: "
        + ("Any" if max_experience is None else f"{max_experience} years")
    )
    output_fn(
        "Explicit work-eligibility filtering: "
        + (
            ", ".join(sorted(work_eligibility.countries))
            if work_eligibility.intent is RuleIntent.MUST
            else "Off"
        )
    )
    output_fn(f"Employment type: {employment_label}")
    output_fn(f"Notes: {notes or 'None'}")

    should_save = _ask(
        prompt="\nSave this search brief? [Y/n]: ",
        parser=_confirmation,
        input_fn=input_fn,
        output_fn=output_fn,
    )
    if not should_save:
        output_fn("Search brief not saved. Run 'python -m job_scout profile create' to restart.")
        return None

    destination = Path(output_dir) / f"{brief.client_id}.json"
    if destination.exists():
        output_fn(f"Search brief not saved: {destination} already exists.")
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(brief.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    output_fn(f"\nSearch brief saved: {destination}")
    output_fn(
        "\nNext:\n"
        f"python -m job_scout collect \\\n  --client {destination} \\\n"
        '  --board <greenhouse-board-token> \\\n  --company "<company-name>" \\\n'
        f"  --csv exports/{brief.client_id}.csv"
    )
    return destination
