"""Attribution scope for upstream-caused failures.

An upstream 5xx forwarded verbatim makes callers think this node is down.
Upstream failures are therefore reported as ``424`` with
``error.code = UPSTREAM_UNAVAILABLE``, the ``X-Routstr-Error-Scope: upstream``
header, and the provider's status in ``upstream_status``. Rate limits keep
``429``. Node faults keep ``500`` and carry no scope header.
"""

from __future__ import annotations

UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
# Deliberately not 5xx: an upstream blip must not read as node health.
UPSTREAM_ERROR_STATUS = 424

ERROR_SCOPE_HEADER = "X-Routstr-Error-Scope"
ERROR_SCOPE_UPSTREAM = "upstream"
ERROR_SCOPE_NODE = "node"


def _is_rate_limit_code(code: object) -> bool:
    # Lazy import: routstr.upstream imports this module.
    from ..upstream.rate_limit import UPSTREAM_RATE_LIMIT

    return code == UPSTREAM_RATE_LIMIT


def client_status_for_upstream_error(
    status_code: int | None, code: object = None
) -> int:
    """Map an upstream status to the one the caller sees: 429 and 4xx pass
    through, 5xx (or unknown) becomes :data:`UPSTREAM_ERROR_STATUS`."""
    if _is_rate_limit_code(code):
        return 429
    if not status_code or status_code >= 500:
        return UPSTREAM_ERROR_STATUS
    return status_code


def client_code_for_upstream_error(
    status_code: int | None, code: str | int | None
) -> str | int | None:
    """Return the ``error.code`` matching :func:`client_status_for_upstream_error`."""
    if _is_rate_limit_code(code):
        return code
    if not status_code or status_code >= 500:
        return UPSTREAM_UNAVAILABLE
    return code


def upstream_status_details(
    details: dict[str, object] | None, upstream_status: int | None
) -> dict[str, object] | None:
    """Add ``upstream_status`` to ``details`` when the caller sees a different status."""
    merged: dict[str, object] = dict(details) if details else {}
    if upstream_status and upstream_status != client_status_for_upstream_error(
        upstream_status
    ):
        merged["upstream_status"] = upstream_status
    return merged or None
