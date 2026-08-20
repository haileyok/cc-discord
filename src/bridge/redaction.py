"""Central redaction and safe operator-facing error formatting."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_MAX_VALUE = 240
_TOKEN_RE = re.compile(r"(?i)\b(?:xox[baprs]-|xapp-|sk-|gh[pousr]_)[A-Za-z0-9._-]+\b")
_BEARER_RE = re.compile(r"(?i)\b(?:authorization\s*:\s*)?bearer\s+[A-Za-z0-9._~+/=-]+")
_QUERY_RE = re.compile(r"([?&](?:token|access_token|api[_-]?key|secret|sig|signature|code|auth)=[^&\s]+)", re.I)
_PATH_RE = re.compile(r"(?<![A-Za-z0-9])/(?:home|tmp|var|etc|Users|private|root)/[^\s'\"]+")
_URL_RE = re.compile(r"https?://[^\s<>\"']+")


def _safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<url>"
    if not parsed.scheme or not parsed.netloc:
        return "<url>"
    host = (parsed.hostname or "").lower()
    if host == "slack.com" or host.endswith(".slack.com") or "slack" in host:
        return "<slack-private-url>"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", ""))


def redact(value: Any, *, limit: int = _MAX_VALUE) -> str:
    """Return a bounded string with credentials, URLs, queries and paths removed."""
    if isinstance(value, Mapping):
        return "<mapping>"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "<binary>"
    text = str(value)
    text = _BEARER_RE.sub("<authorization-redacted>", text)
    text = _TOKEN_RE.sub("<token-redacted>", text)
    text = _URL_RE.sub(lambda match: _safe_url(match.group(0)), text)
    text = _QUERY_RE.sub("<query-redacted>", text)
    text = _PATH_RE.sub("<path-redacted>", text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def safe_error(exc: BaseException | None = None, fallback: str = "operation failed") -> str:
    """Return a fixed, non-sensitive error suitable for Slack/CLI output."""
    status = getattr(exc, "status", None) if exc is not None else None
    if isinstance(status, int) and 400 <= status <= 599:
        return f"{fallback} (HTTP {status})"
    return fallback


def safe_log(value: Any, *, limit: int = _MAX_VALUE) -> str:
    """Alias documenting that a value is being emitted to a log."""
    return redact(value, limit=limit)


__all__ = ["redact", "safe_error", "safe_log"]
