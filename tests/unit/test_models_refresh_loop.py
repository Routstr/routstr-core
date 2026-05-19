"""Regression tests for the periodic upstream models refresh loop."""

from __future__ import annotations

import asyncio
import os
from typing import cast
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")

from routstr.upstream.base import BaseUpstreamProvider  # noqa: E402


class _FakeUpstream:
    """Minimal stand-in for BaseUpstreamProvider used by the refresh loop.

    Only ``base_url`` (for error logging) and ``refresh_models_cache`` (the call
    under test) are exercised; everything else stays unused.
    """

    def __init__(self, name: str) -> None:
        self.base_url = f"http://{name}"
        self.refresh_models_cache = AsyncMock()


def _make_fake_upstream(name: str) -> BaseUpstreamProvider:
    # The loop only uses duck-typed attributes — cast keeps the test type-clean
    # without dragging in BaseUpstreamProvider's full constructor.
    return cast(BaseUpstreamProvider, _FakeUpstream(name))


@pytest.mark.asyncio
async def test_refresh_loop_picks_up_providers_added_after_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a provider is added after the loop starts (e.g. via reinitialize_upstreams),
    the next loop iteration must refresh it. Previously the loop captured the upstream
    list at startup and missed any later additions."""
    from routstr.core.settings import settings as global_settings
    from routstr.upstream.helpers import refresh_upstreams_models_periodically

    # Tight interval so the test finishes quickly.
    monkeypatch.setattr(
        global_settings, "models_refresh_interval_seconds", 1, raising=False
    )

    initial_upstream = _make_fake_upstream("initial")
    live_list: list[BaseUpstreamProvider] = [initial_upstream]

    # Stub out the post-iteration sats-pricing refresh so the loop body has no DB deps.
    async def _noop_pricing_refresh() -> None:  # pragma: no cover - trivial stub
        return None

    monkeypatch.setattr(
        "routstr.payment.models._update_sats_pricing_once",
        _noop_pricing_refresh,
    )

    task = asyncio.create_task(refresh_upstreams_models_periodically(lambda: live_list))

    try:
        # Wait for the first iteration to refresh the initial upstream.
        for _ in range(40):
            if initial_upstream.refresh_models_cache.await_count >= 1:  # type: ignore[attr-defined]
                break
            await asyncio.sleep(0.05)
        assert initial_upstream.refresh_models_cache.await_count >= 1, (  # type: ignore[attr-defined]
            "loop did not refresh the initial upstream within the timeout"
        )

        # Simulate reinitialize_upstreams: replace the live list contents with new
        # provider instances. The loop must observe the swap on its next tick.
        new_upstream = _make_fake_upstream("added-after-startup")
        live_list[:] = [new_upstream]

        for _ in range(60):
            if new_upstream.refresh_models_cache.await_count >= 1:  # type: ignore[attr-defined]
                break
            await asyncio.sleep(0.05)

        assert new_upstream.refresh_models_cache.await_count >= 1, (  # type: ignore[attr-defined]
            "loop did not refresh the upstream added after startup — "
            "regression: list snapshot captured at startup"
        )
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_refresh_loop_disabled_when_interval_non_positive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from routstr.core.settings import settings as global_settings
    from routstr.upstream.helpers import refresh_upstreams_models_periodically

    monkeypatch.setattr(
        global_settings, "models_refresh_interval_seconds", 0, raising=False
    )

    upstream = _make_fake_upstream("never-refreshed")

    # Loop must return immediately without ever touching the upstream.
    await asyncio.wait_for(
        refresh_upstreams_models_periodically(lambda: [upstream]),
        timeout=1.0,
    )
    upstream.refresh_models_cache.assert_not_awaited()  # type: ignore[attr-defined]
