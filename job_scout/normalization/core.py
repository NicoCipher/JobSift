from __future__ import annotations

import hashlib
import html
import json
import re
from html.parser import HTMLParser
from typing import ClassVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from job_scout.domain.models import EmploymentType, RemoteStatus

TRACKING_PARAMETERS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source"}


class _TextExtractor(HTMLParser):
    BLOCKS: ClassVar[set[str]] = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style"}:
            self.ignored_depth += 1
            return
        if self.ignored_depth:
            return
        if tag.lower() in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"}:
            self.ignored_depth = max(0, self.ignored_depth - 1)
            return
        if self.ignored_depth:
            return
        if tag.lower() in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def html_to_text(value: str | None) -> str | None:
    if value is None:
        return None
    decoded = html.unescape(value)
    parser = _TextExtractor()
    parser.feed(decoded)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line) or None


def canonicalize_url(value: str) -> str:
    parts = urlsplit(value.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMETERS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def normalize_title(value: str) -> str:
    value = value.casefold().replace("full-stack", "full stack")
    value = re.sub(r"[^a-z0-9+#.]+", " ", value)
    return " ".join(value.split())


def classify_remote(location: str | None, description: str | None) -> RemoteStatus:
    loc = (location or "").casefold()
    body = (description or "").casefold()
    combined = f"{loc} {body}"
    if re.search(r"\b(hybrid|days? (?:a|per) week (?:in|on)[ -]?office)\b", combined):
        return RemoteStatus.HYBRID
    if re.search(r"\b(on[ -]?site|in[ -]?office)\b", combined):
        return RemoteStatus.ONSITE
    if re.search(r"\bremote\b", combined):
        return RemoteStatus.REMOTE
    return RemoteStatus.UNKNOWN


def content_fingerprint(
    *,
    title: str,
    description: str | None,
    location: str | None,
    employment_type: EmploymentType | None,
) -> str:
    payload = {
        "title": normalize_title(title),
        "description": re.sub(r"\s+", " ", (description or "").strip()),
        "location": re.sub(r"\s+", " ", (location or "").casefold().strip()),
        "employment_type": employment_type.value if employment_type else None,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
