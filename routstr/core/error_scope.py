"""Attribution scope for upstream-caused failures.

routstr-core fronts third-party inference providers. When one of them fails,
this node is still healthy: it accepted the request, authenticated it, reserved
payment, and reverted the reservation once the last candidate failed. Forwarding
the provider's 5xx verbatim makes a caller conclude *this node* is down, so it
marks the node down, drops it from rotation, or refuses to retry an upstream
blip.

Upstream-attributed failures therefore answer a deliberate non-5xx status —
``424 Failed Dependency`` — carrying the stable ``UPSTREAM_UNAVAILABLE``
``error.code``, the ``X-Routstr-Error-Scope: upstream`` response header, and the
provider's own status preserved in ``error.details.upstream_status``. Rate
limits keep ``429`` with their existing ``UPSTREAM_RATE_LIMIT`` code: the retry
hint is worth more there than the status class, and clients already branch on
that pair.

Genuine node faults are deliberately untouched: an unreachable mint, a DB
failure, or an unhandled exception still answers ``500`` and carries no scope
header, so "node healthy, upstream failed" and "node broken" stay
distinguishable from the response alone.
"""

from __future__ import annotations

#: Stable, machine-readable classification for an upstream-attributable
#: failure (``error.code``). Mirrors the ``UPSTREAM_RATE_LIMIT`` precedent: a
#: non-numeric code clients can branch on without parsing provider messages.
UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"

#: HTTP status returned for upstream-attributable failures. 424 is deliberately
#: not 5xx so callers stop reading an upstream blip as this node's health.
UPSTREAM_ERROR_STATUS = 424

#: Header naming the failure's attribution scope. Present (value ``upstream``)
#: on every upstream-caused error path; absent on node faults.
ERROR_SCOPE_HEADER = "X-Routstr-Error-Scope"
ERROR_SCOPE_UPSTREAM = "upstream"
ERROR_SCOPE_NODE = "node"


def _is_rate_limit_code(code: object) -> bool:
    """Return whether ``code`` is the upstream rate-limit classification."""
    # Imported lazily: ``routstr.upstream`` imports the proxy/payment stack, and
    # this module is imported from both sides of it.
    from ..upstream.rate_limit import UPSTREAM_RATE_LIMIT

    return code == UPSTREAM_RATE_LIMIT


def client_status_for_upstream_error(
    status_code: int | None, code: object = None
) -> int:
    """Return the HTTP status a caller sees for an upstream-attributable failure.

    ``status_code`` is the provider's own status (or the status the failure was
    classified with). Rate limits keep 429; every other 5xx collapses to
    :data:`UPSTREAM_ERROR_STATUS`; 4xx statuses pass through unchanged because
    they are the provider's verdict on the *request*, not a node-health signal.
    """
    if _is_rate_limit_code(code):
        return 429
    if not status_code or status_code >= 500:
        return UPSTREAM_ERROR_STATUS
    return status_code


def client_code_for_upstream_error(
    status_code: int | None, code: str | int | None
) -> str | int | None:
    """Return the ``error.code`` a caller sees for an upstream-attributable failure."""
    if _is_rate_limit_code(code):
        return code
    if not status_code or status_code >= 500:
        return UPSTREAM_UNAVAILABLE
    return code


def upstream_status_details(
    details: dict[str, object] | None, upstream_status: int | None
) -> dict[str, object] | None:
    """Return ``details`` with the provider's status preserved when it differs.

    Only recorded when the caller-visible status no longer equals the status the
    upstream hop produced — otherwise the information is already on the status
    line and would just be noise.
    """
    merged: dict[str, object] = dict(details) if details else {}
    if upstream_status and upstream_status != client_status_for_upstream_error(
        upstream_status
    ):
        merged["upstream_status"] = upstream_status
    return merged or None
