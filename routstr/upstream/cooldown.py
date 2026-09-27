"""In-memory circuit breaker for a failing (provider, model) pair.

Process-local by design: each node observes its own upstream failures, and a
cooldown that outlives a restart would hide a provider that has recovered.
"""

from __future__ import annotations

import time

from ..core import get_logger
from ..core.settings import settings

logger = get_logger(__name__)

_FAILURE_WINDOW_SECONDS = 60.0

_failures: dict[tuple[str, str], list[float]] = {}
_cooling_until: dict[tuple[str, str], float] = {}


def record_failure(provider_id: str, model_id: str) -> None:
    """Count a timeout or 5xx, opening a cooldown once too many land in a minute."""
    if settings.upstream_cooldown_seconds <= 0:
        return

    pair = (provider_id, model_id)
    now = time.monotonic()
    recent = [t for t in _failures.get(pair, []) if now - t < _FAILURE_WINDOW_SECONDS]
    recent.append(now)

    if len(recent) >= settings.upstream_allowed_fails:
        _failures.pop(pair, None)
        _cooling_until[pair] = now + settings.upstream_cooldown_seconds
        logger.warning(
            "Upstream cooling down after repeated failures",
            extra={
                "provider": provider_id,
                "model": model_id,
                "cooldown_seconds": settings.upstream_cooldown_seconds,
            },
        )
    else:
        _failures[pair] = recent


def is_cooling_down(provider_id: str, model_id: str) -> bool:
    pair = (provider_id, model_id)
    until = _cooling_until.get(pair)
    if until is None:
        return False
    if time.monotonic() >= until:
        del _cooling_until[pair]
        return False
    return True


def reset_cooldowns() -> None:
    _failures.clear()
    _cooling_until.clear()
