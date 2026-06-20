"""Redaction helpers for sensitive provider identifiers.

Single source of truth for stripping account-scoped identifiers (e.g. OpenAI
organization IDs) from any text before it is logged, returned to a caller, or
written to an audit entry.
"""

from __future__ import annotations

import re
from typing import Any

# OpenAI-style organization identifiers look like ``org-<base62>``. Require at
# least 6 trailing chars so the already-redacted literal ``org-[REDACTED]`` is
# never re-matched (``[`` is not in the character class).
_ORG_ID_PATTERN = re.compile(r"\borg-[A-Za-z0-9]{6,}\b")

ORG_ID_PLACEHOLDER = "org-[REDACTED]"


def redact_org_ids(text: str) -> str:
    """Replace OpenAI-style organization IDs with ``org-[REDACTED]``.

    Args:
        text: Arbitrary text that may embed an ``org-*`` identifier.

    Returns:
        The text with every organization ID replaced. Non-string input is
        returned unchanged after coercion to ``str``.
    """
    if not text:
        return text
    return _ORG_ID_PATTERN.sub(ORG_ID_PLACEHOLDER, text)


def redact_obj(obj: Any) -> Any:
    """Recursively redact organization IDs in arbitrary nested structures.

    Strings are redacted in place; dicts and lists/tuples are walked so that
    identifiers nested inside structured payloads (e.g. log ``extra`` fields or
    error ``details``) are also stripped. Other types are returned unchanged.
    """
    if isinstance(obj, str):
        return redact_org_ids(obj)
    if isinstance(obj, dict):
        return {key: redact_obj(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(value) for value in obj]
    if isinstance(obj, tuple):
        return tuple(redact_obj(value) for value in obj)
    return obj
