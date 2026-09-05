from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class ExperienceEvidenceKind(StrEnum):
    MANDATORY = "mandatory"
    PREFERRED = "preferred"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ExperienceEvidence:
    minimum_years: int
    kind: ExperienceEvidenceKind
    source_text: str


@dataclass(frozen=True)
class ExperienceAssessment:
    evidence: tuple[ExperienceEvidence, ...]

    @property
    def mandatory_minimum_years(self) -> int | None:
        mandatory = [
            item.minimum_years
            for item in self.evidence
            if item.kind is ExperienceEvidenceKind.MANDATORY
        ]
        return max(mandatory, default=None)


_EXPERIENCE_PATTERN = re.compile(
    r"""
    (?:(?P<at_least>at\s+least)\s+(?P<at_least_years>\d{1,2})
      |(?P<minimum>(?:a\s+)?minimum(?:\s+of)?)\s+(?P<minimum_years>\d{1,2})
      |(?P<range_years>\d{1,2})\s*(?:-|\N{EN DASH}|\N{EM DASH}|to)\s*\d{1,2}
      |(?P<or_more_years>\d{1,2})\s+or\s+more
      |(?P<plus_years>\d{1,2})\s*\+
      |(?P<plain_years>\d{1,2}))
    \s*years?
    """,
    re.IGNORECASE | re.VERBOSE,
)
_EXPERIENCE_CONTEXT = re.compile(
    r"\b(?:experience|experienced|working\s+(?:in|with|as)|work\s+(?:in|with|as)|"
    r"years?\s+in\s+(?:an?\s+)?(?:role|field|domain|technical\s+support|customer\s+support))\b",
    re.IGNORECASE,
)
_PREFERRED_CONTEXT = re.compile(
    r"\b(?:preferred|ideally|nice[- ]to[- ]have|bonus|desirable|(?:is|would\s+be)\s+a\s+plus)\b",
    re.IGNORECASE,
)
_AMBIGUOUS_CONTEXT = re.compile(
    r"\b(?:about|around|approximately|roughly|typically)\b", re.IGNORECASE
)
_MANDATORY_CONTEXT = re.compile(
    r"\b(?:required|requirement|must|minimum|at\s+least)\b", re.IGNORECASE
)
_UNRELATED_CONTEXT = re.compile(
    r"\b(?:years?\s+ago|years?\s+old|founded|company\s+history|product\s+history|"
    r"has\s+existed|have\s+existed|in\s+business)\b",
    re.IGNORECASE,
)


def _segments(text: str) -> list[str]:
    return [
        segment.strip(" \t-*\u2022")
        for segment in re.split(r"(?:\r?\n|(?<=[.!?;])\s+)", text)
        if segment.strip(" \t-*\u2022")
    ]


def _minimum_years(match: re.Match[str]) -> int:
    for group in (
        "at_least_years",
        "minimum_years",
        "range_years",
        "or_more_years",
        "plus_years",
        "plain_years",
    ):
        value = match.group(group)
        if value is not None:
            return int(value)
    raise AssertionError("experience pattern matched without a year value")


def extract_experience_evidence(*texts: str | None) -> ExperienceAssessment:
    evidence: list[ExperienceEvidence] = []
    for text in texts:
        if not text:
            continue
        for segment in _segments(text):
            for match in _EXPERIENCE_PATTERN.finditer(segment):
                if _UNRELATED_CONTEXT.search(segment):
                    continue

                has_strong_quantifier = match.group("plain_years") is None
                has_experience_context = bool(_EXPERIENCE_CONTEXT.search(segment))
                has_mandatory_context = bool(_MANDATORY_CONTEXT.search(segment))
                has_preferred_context = bool(_PREFERRED_CONTEXT.search(segment))
                has_ambiguous_context = bool(_AMBIGUOUS_CONTEXT.search(segment))

                if not (
                    has_strong_quantifier
                    or has_experience_context
                    or has_mandatory_context
                    or has_preferred_context
                ):
                    continue
                if has_preferred_context:
                    kind = ExperienceEvidenceKind.PREFERRED
                elif has_ambiguous_context:
                    kind = ExperienceEvidenceKind.AMBIGUOUS
                elif has_strong_quantifier or has_mandatory_context or has_experience_context:
                    kind = ExperienceEvidenceKind.MANDATORY
                else:
                    kind = ExperienceEvidenceKind.AMBIGUOUS

                evidence.append(
                    ExperienceEvidence(
                        minimum_years=_minimum_years(match),
                        kind=kind,
                        source_text=segment,
                    )
                )
    return ExperienceAssessment(evidence=tuple(evidence))


def extract_required_experience_years(*texts: str | None) -> int | None:
    return extract_experience_evidence(*texts).mandatory_minimum_years
