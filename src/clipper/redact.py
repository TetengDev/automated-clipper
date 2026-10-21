"""Scrub credentials out of free text (error messages) before it is stored or logged."""

from __future__ import annotations

import re
from collections.abc import Callable

MASK = "REDACTED"

_Replacement = str | Callable[[re.Match[str]], str]

_PATTERNS: tuple[tuple[re.Pattern[str], _Replacement], ...] = (
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
    # key=value / key: value credentials in tool output (rclone, S3 errors, JSON configs).
    # The key prefix is bounded ({0,40}) so matching stays linear on long runs of [\w-].
    # Quoted values are masked up to the closing quote (values may contain spaces).
    (
        re.compile(
            r"(?<![A-Za-z0-9])("
            r"[\w-]{0,40}(?:secret(?:[_-]access)?(?:[_-]key)?|token|password|passwd"
            r"|passphrase|signature|api[_-]?key)"
            r"|access[_-]key[_-]id|pass"
            r")(['\"]?\s*[:=]\s*)(?:(['\"])[^'\"\n]*\3|[^\s'\"&,;]+)",
            re.IGNORECASE,
        ),
        lambda m: f"{m[1]}{m[2]}{m[3] or ''}{MASK}{m[3] or ''}",
    ),
)


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
