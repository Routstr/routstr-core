"""Reactive request-correction layer.

When an upstream rejects a request with a recoverable 4xx error, this layer
tries to *fix* the request body and let the caller retry the same upstream
instead of failing outright. It is provider-agnostic: correctors key off the
upstream's own error wording, so the same recovery works across every provider.

The layer is a small pipeline of :data:`Corrector` callables. Each corrector
inspects the parsed request body and the upstream error message and either
returns a corrected body (plus a short label identifying the fix) or declines
by returning ``None``. Adding a new reactive fix means writing one corrector
and adding it to :data:`DEFAULT_CORRECTORS` — no changes to the proxy loop.

All corrections are immutable: a corrector never mutates the body it is given,
it returns a new ``dict``. The proxy threads an ``applied`` set of fix labels
through retries so each distinct fix is applied at most once, guaranteeing the
retry loop always terminates.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from fastapi.responses import Response

from ..core import get_logger

logger = get_logger(__name__)


# Matches upstream error text that names a single rejected request parameter,
# e.g. "`temperature` is deprecated for this model." or
# "parameter 'top_p' is not supported". Keys off the upstream's own wording so
# a 400 about an unsupported sampling/option field can be recovered by stripping
# that field and retrying the same upstream.
_UNSUPPORTED_PARAM_RE = re.compile(
    r"[`'\"]?(?P<param>[a-zA-Z_][a-zA-Z0-9_]*)[`'\"]?\s+is\s+"
    r"(?:deprecated|not\s+supported|unsupported|no\s+longer\s+supported)",
    re.IGNORECASE,
)


# Matches upstream error text that rejects a param and names its replacement,
# e.g. OpenAI's "Unsupported parameter: 'max_tokens' is not supported with this
# model. Use 'max_completion_tokens' instead." Both names must be quoted so a
# free-form hint like "use gpt-4 instead" never reads as a rename.
_RENAMED_PARAM_RE = re.compile(
    r"[`'\"](?P<param>[a-zA-Z_][a-zA-Z0-9_]*)[`'\"]\s+is\s+"
    r"(?:deprecated|not\s+supported|unsupported|no\s+longer\s+supported)\b"
    r".*?\buse\s+[`'\"](?P<replacement>[a-zA-Z_][a-zA-Z0-9_]*)[`'\"]\s+instead",
    re.IGNORECASE | re.DOTALL,
)


# A corrector inspects the parsed request body and the upstream error message
# and returns ``(new_body_dict, label)`` for a fix it can apply, or ``None`` to
# decline. ``label`` identifies the fix so it is applied at most once per request.
Corrector = Callable[[dict, str], "tuple[dict, str] | None"]


@dataclass(frozen=True)
class Correction:
    """A successful request correction ready to retry.

    ``body`` is the corrected JSON body (encoded), ``label`` identifies the fix
    that was applied (e.g. the stripped param name) so the caller can guard
    against applying the same fix twice.
    """

    body: bytes
    label: str


def extract_error_message(response: Response) -> str:
    """Best-effort extraction of an error message string from a proxy Response."""
    body_bytes = getattr(response, "body", None)
    if not body_bytes:
        return ""
    try:
        data = json.loads(body_bytes)
    except Exception:
        return body_bytes.decode("utf-8", errors="ignore")[:500]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("detail")
            if isinstance(msg, str):
                return msg
        elif isinstance(err, str):
            return err
        if isinstance(data.get("message"), str):
            return data["message"]
    return ""


# Spend-shaping fields bound how much work — and therefore cost — the upstream
# may perform. The reservation was priced with these caps in place; dropping one
# and retrying would let the request run uncapped (or fan out) and bill above the
# caller's authorization. When the upstream names one of these, decline the strip
# and let the error propagate. Matched case-insensitively.
_SPEND_SHAPING_PARAMS = frozenset(
    {
        "max_tokens",
        "max_completion_tokens",
        "max_output_tokens",
        "max_tokens_to_sample",
        "n",
        "best_of",
    }
)

# Output caps are interchangeable spellings of the same limit, so moving the
# value from one to another keeps the priced bound intact.
_OUTPUT_CAP_PARAMS = frozenset(
    {
        "max_tokens",
        "max_completion_tokens",
        "max_output_tokens",
        "max_tokens_to_sample",
    }
)


def rename_unsupported_param(body: dict, error_message: str) -> tuple[dict, str] | None:
    """Move a rejected top-level param to the name the upstream asked for.

    Returns ``(new_body, label)`` with the value carried over unchanged, or
    ``None`` when the error names no replacement, the param is absent, or the
    replacement is already set.

    A spend-shaping field is only renamed to another output cap: that keeps the
    reservation's bound, whereas renaming into or out of any other spend-shaping
    field could uncap or fan out the retry.
    """
    match = _RENAMED_PARAM_RE.search(error_message)
    if not match:
        return None
    param, replacement = match.group("param"), match.group("replacement")
    if param == replacement or param not in body or replacement in body:
        return None
    param_spend = param.lower() in _SPEND_SHAPING_PARAMS
    replacement_spend = replacement.lower() in _SPEND_SHAPING_PARAMS
    if (param_spend or replacement_spend) and not (
        param.lower() in _OUTPUT_CAP_PARAMS
        and replacement.lower() in _OUTPUT_CAP_PARAMS
    ):
        logger.warning(
            "Upstream asked to rename '%s' to '%s'; refusing because it would "
            "change the request's spend bound — surfacing the error",
            param,
            replacement,
        )
        return None
    new_body = {(replacement if k == param else k): v for k, v in body.items()}
    return new_body, f"{param}->{replacement}"


def strip_unsupported_param(body: dict, error_message: str) -> tuple[dict, str] | None:
    """Drop a top-level param the upstream named as unsupported/deprecated.

    Returns ``(new_body, param)`` (a new dict, original untouched) when the
    error names a top-level param present in the body, otherwise ``None``.

    Spend-shaping fields (output caps, fan-out counts) are never stripped:
    removing one after the reservation was priced would uncap the retry and
    overcharge. When the upstream names such a field, decline so the original
    error propagates rather than silently resizing the request's cost.
    """
    match = _UNSUPPORTED_PARAM_RE.search(error_message)
    if not match:
        return None
    param = match.group("param")
    if param not in body:
        return None
    if param.lower() in _SPEND_SHAPING_PARAMS:
        logger.warning(
            "Upstream rejected spend-shaping param '%s'; refusing to strip it "
            "(retrying uncapped would overcharge) — surfacing the error",
            param,
        )
        return None
    new_body = {k: v for k, v in body.items() if k != param}
    return new_body, param


# Ordered pipeline of correctors tried on each recoverable rejection.
# Renaming runs first so a param with a named replacement keeps its value
# instead of being dropped.
DEFAULT_CORRECTORS: tuple[Corrector, ...] = (
    rename_unsupported_param,
    strip_unsupported_param,
)


def correct_request(
    request_body: bytes,
    error_message: str,
    applied: set[str],
    correctors: Sequence[Corrector] = DEFAULT_CORRECTORS,
) -> Correction | None:
    """Try to correct a rejected request body so it can be retried.

    Runs each corrector in order against the parsed body and ``error_message``.
    The first corrector that proposes a fix whose ``label`` is not already in
    ``applied`` wins; its result is returned as a :class:`Correction`. Returns
    ``None`` when nothing parses, nothing matches, or every proposed fix was
    already applied — the caller then treats the response as a normal failure.

    ``applied`` is read-only here; the caller records the returned ``label`` to
    bound retries and guarantee forward progress.
    """
    if not request_body or not error_message:
        return None
    try:
        data = json.loads(request_body)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    for corrector in correctors:
        result = corrector(data, error_message)
        if result is None:
            continue
        new_body, label = result
        if label in applied:
            continue
        return Correction(body=json.dumps(new_body).encode(), label=label)
    return None
