from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

US_STATE_CODES = frozenset(
    [
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
        "DC",
    ]
)
US_COUNTRY_PATTERN = re.compile(
    r"(?<![A-Za-z])(?:United States(?: of America)?|U\.S\.A\.?|USA|U\.S\.)(?![A-Za-z])",
    re.IGNORECASE,
)
CITY_STATE_PATTERN = re.compile(
    r"^\s*([^,]+?)\s*,\s*([A-Za-z]{2})(?:\s*,\s*(?:United States|USA|US))?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class NormalizedLocation:
    country: str | None = None
    region: str | None = None
    city: str | None = None


def normalize_location(
    location_text: str | None, office_locations: Iterable[str] = ()
) -> NormalizedLocation:
    """Normalize only explicit first-pass US evidence; vague regions remain unknown."""
    primary = (location_text or "").strip()
    match = CITY_STATE_PATTERN.fullmatch(primary)
    if match and match.group(2).upper() in US_STATE_CODES:
        city = match.group(1).strip()
        if city.casefold() not in {"remote", "hybrid", "onsite", "on-site"}:
            return NormalizedLocation(
                country="United States", region=match.group(2).upper(), city=city
            )

    evidence = [primary, *(value.strip() for value in office_locations if value.strip())]
    if any(US_COUNTRY_PATTERN.search(value) for value in evidence):
        return NormalizedLocation(country="United States")
    return NormalizedLocation()
