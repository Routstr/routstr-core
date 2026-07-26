"""Detection and parsing of upstream provider rate-limit errors.

Upstream OpenAI-compatible providers signal rate limits via HTTP 429 and/or a
human-readable message such as::

    Rate limit reached for gpt-5.5-2026-04-23 (for limit gpt-5.5) in organization
    org-XXXX on tokens per min (TPM): Limit 180000000, Used 180000000,
    Requested 8929. Please try again in 2ms.

This module classifies those failures into a stable :data:`UPSTREAM_RATE_LIMIT`
code and extracts useful debugging fields. All retained text is redacted of
organization IDs first.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from ..core.redaction import redact_org_ids

# Stable error code callers can switch on to distinguish upstream rate limits
# from generic request failures. The literal value matches the identifier named
# in issue #555 ("UPSTREAM_RATE_LIMIT") so the public API contract is exact.
UPSTREAM_RATE_LIMIT = "UPSTREAM_RATE_LIMIT"

# Message fragments that indicate a rate-limit even when the status code is not
# 429 (some providers wrap it in a 400/500 envelope).
_RATE_LIMIT_MARKERS = (
    "rate limit reached",
    "rate_limit_exceeded",
    "rate limit exceeded",
    "too many requests",
)

_MODEL_RE = re.compile(r"Rate limit reached for ([^\s(]+)", re.IGNORECASE)
_LIMIT_NAME_RE = re.compile(r"\(for limit ([^)]+)\)", re.IGNORECASE)
_METRIC_RE = re.compile(r"on ([a-z ]+\((?:TPM|RPM|TPD|RPD|IPM)\))", re.IGNORECASE)
_LIMIT_RE = re.compile(r"Limit (\d+)", re.IGNORECASE)
_USED_RE = re.compile(r"Used (\d+)", re.IGNORECASE)
_REQUESTED_RE = re.compile(r"Requested (\d+)", re.IGNORECASE)
_RETRY_RE = re.compile(r"try again in ([\d.]+)\s*(ms|s)", re.IGNORECASE)


@dataclass
class RateLimitInfo:
    """Structured, redaction-safe view of an upstream rate-limit error."""

    code: str
    message: str
    model: str | None = None
    limit_name: str | None = None
    metric: str | None = None
    limit: int | None = None
    used: int | None = None
    requested: int | None = None
    retry_after_seconds: float | None = None

    def as_details(self) -> dict[str, object]:
        """Return a JSON-serialisable dict for embedding in an error envelope."""
        return {k: v for k, v in asdict(self).items() if v is not None}


def _looks_like_rate_limit(status_code: int, message: str) -> bool:
    if status_code == 429:
        return True
    lowered = message.lower()
    return any(marker in lowered for marker in _RATE_LIMIT_MARKERS)


def _parse_retry_after_header(headers: dict[str, str] | None) -> float | None:
    """Parse a ``Retry-After`` header (delta-seconds form) into seconds."""
    if not headers:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _int_or_none(match: re.Match[str] | None) -> int | None:
    if match is None:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def classify_rate_limit(
    status_code: int,
    message: str,
    headers: dict[str, str] | None = None,
) -> RateLimitInfo | None:
    """Classify an upstream error as a rate-limit and extract its fields.

    Args:
        status_code: HTTP status code from the upstream response.
        message: Upstream error message (may contain sensitive identifiers).
        headers: Optional upstream response headers, used for ``Retry-After``.

    Returns:
        A :class:`RateLimitInfo` when the error is a rate-limit, else ``None``.
    """
    message = message or ""
    if not _looks_like_rate_limit(status_code, message):
        return None

    redacted = redact_org_ids(message)

    model_match = _MODEL_RE.search(redacted)
    metric_match = _METRIC_RE.search(redacted)

    retry_after = _parse_retry_after_header(headers)
    if retry_after is None:
        retry_match = _RETRY_RE.search(redacted)
        if retry_match is not None:
            value = float(retry_match.group(1))
            retry_after = value / 1000.0 if retry_match.group(2).lower() == "ms" else value

    limit_name_match = _LIMIT_NAME_RE.search(redacted)

    return RateLimitInfo(
        code=UPSTREAM_RATE_LIMIT,
        message=redacted,
        model=model_match.group(1) if model_match else None,
        limit_name=limit_name_match.group(1).strip() if limit_name_match else None,
        metric=metric_match.group(1).strip() if metric_match else None,
        limit=_int_or_none(_LIMIT_RE.search(redacted)),
        used=_int_or_none(_USED_RE.search(redacted)),
        requested=_int_or_none(_REQUESTED_RE.search(redacted)),
        retry_after_seconds=retry_after,
    )
