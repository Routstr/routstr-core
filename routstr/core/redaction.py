"""Redaction helpers for sensitive provider identifiers and credentials.

Single source of truth for stripping account-scoped identifiers (e.g. OpenAI
organization IDs) and spendable credentials (Cashu tokens, bearer keys, key
hashes) from any text before it is logged, returned to a caller, or written to
an audit entry.
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


SECRET_PLACEHOLDER = "[REDACTED]"

# Field names whose value is spendable or authenticating on its own. Matched as
# substrings of the lowercased key, so ``hashed_key`` (a live ``sk-`` credential)
# is stripped while the truncated ``key_hash`` prefix used for correlation is
# not. Numeric values are never stripped, which keeps ``input_tokens`` and the
# other usage-analytics fields intact.
_SECRET_KEY_HINTS = (
    "authorization",
    "api_key",
    "apikey",
    "bearer",
    "cashu",
    "cookie",
    "credential",
    "hashed_key",
    "mnemonic",
    "nsec",
    "passphrase",
    "password",
    "private_key",
    "privkey",
    "secret",
    "token",
)

# Value shapes that are spendable wherever they appear, including inside URLs,
# query strings and free-form error text. Every alternative is anchored on a
# literal prefix and uses a single bounded character class, so matching stays
# linear on the logging hot path.
_SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"cashu[A-Z][A-Za-z0-9_\-=/+]{20,}"),
    re.compile(r"\bnsec1[a-z0-9]{20,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9_\-.=]{10,}", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}"),
    re.compile(r"\b[0-9a-f]{64}\b"),
    re.compile(
        r"(?<=[?&])([^=&\s]*(?:token|key|secret|password|auth|sig)[^=&\s]*=)[^&\s\"']+",
        re.IGNORECASE,
    ),
)

_MAX_REDACTION_DEPTH = 12


def _redact_secret_text(text: str) -> str:
    for pattern in _SECRET_VALUE_PATTERNS:
        text = pattern.sub(
            lambda match: (match.group(1) if match.groups() else "")
            + SECRET_PLACEHOLDER,
            text,
        )
    return redact_org_ids(text)


def _is_secret_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(hint in lowered for hint in _SECRET_KEY_HINTS)


def _redact_secrets(obj: Any, depth: int, seen: frozenset[int]) -> Any:
    if isinstance(obj, str):
        return _redact_secret_text(obj)
    if not isinstance(obj, (dict, list, tuple)):
        return obj
    if depth >= _MAX_REDACTION_DEPTH or id(obj) in seen:
        return SECRET_PLACEHOLDER
    nested = seen | {id(obj)}
    if isinstance(obj, dict):
        return {
            key: _redact_value(key, value, depth + 1, nested)
            for key, value in obj.items()
        }
    redacted = [_redact_secrets(value, depth + 1, nested) for value in obj]
    return redacted if isinstance(obj, list) else tuple(redacted)


def _redact_value(key: object, value: Any, depth: int, seen: frozenset[int]) -> Any:
    # Containers keep being walked even under a secret-shaped key so that the
    # surrounding structure stays readable for operators.
    if isinstance(value, (bool, int, float, dict, list, tuple)) or value is None:
        return _redact_secrets(value, depth, seen)
    if _is_secret_key(key):
        return SECRET_PLACEHOLDER
    return _redact_secrets(value, depth, seen)


def redact_field(key: str, value: Any) -> Any:
    """Strip credentials from one named field, e.g. a log ``extra`` entry.

    Both secret-shaped keys and secret-shaped values are stripped, so a leak
    survives neither a renamed field nor a credential embedded in free text.
    The walk is depth-limited and cycle-safe: a malformed payload degrades to
    ``[REDACTED]`` rather than taking the logging call down with it.
    """
    return _redact_value(key, value, 0, frozenset())
