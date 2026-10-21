"""Scrub credentials out of free text (error messages) before it is stored or logged."""

from __future__ import annotations

import re

MASK = "REDACTED"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"([?&](?:key|api_key|token|access_token|client_secret|sig|signature)=)[^&#\s'\"]+",
            re.IGNORECASE,
        ),
        rf"\1{MASK}",
    ),
    (re.compile(r"(/api(?:/v\d+)?/webhooks/\d+/)[\w-]+"), rf"\1{MASK}"),
    (re.compile(r"(hooks\.slack\.com/services/)[\w/-]+"), rf"\1{MASK}"),
    (re.compile(r"(authorization['\"]?\s*[:=]\s*['\"]?)[^'\"\r\n]+", re.IGNORECASE), rf"\1{MASK}"),
    (re.compile(r"(\bbearer\s+)[\w.~+/=-]+", re.IGNORECASE), rf"\1{MASK}"),
    (re.compile(r"(?<![A-Za-z0-9])(?:sk-|gsk_)[\w-]{8,}"), MASK),
)


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
