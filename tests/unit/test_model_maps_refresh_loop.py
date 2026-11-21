"""The periodic model-maps rebuild is configurable and can be paused.

It used to be a hardcoded ``sleep(60)`` started unconditionally, so an operator
who set ``MODELS_REFRESH_INTERVAL_SECONDS=0`` to pause model refresh still had
every worker rebuilding the full mapping table once a minute -- including a
provider/model select with ``selectinload`` and a cost ranking over the whole
catalog -- on data that changes rarely.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable

import pytest

from routstr import proxy


@pytest.fixture
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> Callable[[int], list[float]]:
    """Install a sleep that records requested delays and stops after ``limit``."""

    def _install(limit: int) -> list[float]:
        sleeps: list[float] = []

        async def _sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) >= limit:
                raise asyncio.CancelledError

        monkeypatch.setattr(proxy.asyncio, "sleep", _sleep)
        return sleeps

    return _install


@pytest.fixture
def rebuilds(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    calls: list[None] = []

    async def _fake_refresh() -> None:
        calls.append(None)

    monkeypatch.setattr(proxy, "refresh_model_maps", _fake_refresh)
    monkeypatch.setattr(random, "uniform", lambda a, b: 0.0)
    return calls


@pytest.mark.asyncio
async def test_loop_uses_the_configured_interval(
    monkeypatch: pytest.MonkeyPatch,
    fast_sleep: Callable[[int], list[float]],
    rebuilds: list[None],
) -> None:
    from routstr.core.settings import settings

    monkeypatch.setattr(settings, "enable_model_maps_refresh", True, raising=False)
    monkeypatch.setattr(
        settings, "model_maps_refresh_interval_seconds", 300, raising=False
    )

    sleeps = fast_sleep(2)
    await proxy.refresh_model_maps_periodically()

    assert sleeps == [300, 300], "cadence must follow the setting, not a literal 60"
    # The loop waits before rebuilding, so the second sleep (which stops the
    # test) lands before a second rebuild.
    assert len(rebuilds) == 1


@pytest.mark.asyncio
async def test_zero_interval_idles_instead_of_rebuilding(
    monkeypatch: pytest.MonkeyPatch,
    fast_sleep: Callable[[int], list[float]],
    rebuilds: list[None],
) -> None:
    """``0`` must pause the refresh, and must not exit the task: an operator
    raising it back to N later has to take effect without a restart."""
    from routstr.core.settings import settings

    monkeypatch.setattr(settings, "enable_model_maps_refresh", True, raising=False)
    monkeypatch.setattr(
        settings, "model_maps_refresh_interval_seconds", 0, raising=False
    )

    sleeps = fast_sleep(3)
    await proxy.refresh_model_maps_periodically()

    assert rebuilds == [], "a paused loop must not rebuild the maps"
    assert len(sleeps) == 3, "a paused loop must keep polling, not return"


@pytest.mark.asyncio
async def test_kill_switch_pauses_the_loop(
    monkeypatch: pytest.MonkeyPatch,
    fast_sleep: Callable[[int], list[float]],
    rebuilds: list[None],
) -> None:
    from routstr.core.settings import settings

    monkeypatch.setattr(settings, "enable_model_maps_refresh", False, raising=False)
    monkeypatch.setattr(
        settings, "model_maps_refresh_interval_seconds", 60, raising=False
    )

    sleeps = fast_sleep(2)
    await proxy.refresh_model_maps_periodically()

    assert rebuilds == []
    assert len(sleeps) == 2


@pytest.mark.asyncio
async def test_loop_rereads_the_interval_each_iteration(
    monkeypatch: pytest.MonkeyPatch,
    fast_sleep: Callable[[int], list[float]],
    rebuilds: list[None],
) -> None:
    """A run-time change to the interval (including 0 -> N) applies live."""
    from routstr.core.settings import settings

    monkeypatch.setattr(settings, "enable_model_maps_refresh", True, raising=False)
    monkeypatch.setattr(
        settings, "model_maps_refresh_interval_seconds", 0, raising=False
    )

    seen: list[float] = []

    async def _sleep(seconds: float) -> None:
        seen.append(seconds)
        # Stay paused for the first two polls, then re-enable at 120s.
        if len(seen) == 2:
            monkeypatch.setattr(
                settings, "model_maps_refresh_interval_seconds", 120, raising=False
            )
        if len(seen) >= 5:
            raise asyncio.CancelledError

    monkeypatch.setattr(proxy.asyncio, "sleep", _sleep)

    await proxy.refresh_model_maps_periodically()

    assert seen[:2] == [60.0, 60.0], "paused loop polls at the idle cadence"
    assert seen[2:] == [120, 120, 120], "0 -> N takes effect without a restart"
    assert len(rebuilds) == 2


def test_interval_helper_respects_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from routstr.core.settings import settings

    monkeypatch.setattr(settings, "enable_model_maps_refresh", False, raising=False)
    monkeypatch.setattr(
        settings, "model_maps_refresh_interval_seconds", 60, raising=False
    )
    assert proxy._model_maps_refresh_interval_seconds() == 0.0

    monkeypatch.setattr(settings, "enable_model_maps_refresh", True, raising=False)
    assert proxy._model_maps_refresh_interval_seconds() == 60.0


def test_default_interval_keeps_historical_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default stays 60s so existing nodes see no behaviour change on upgrade."""
    from routstr.core.settings import Settings

    field = Settings.__fields__["model_maps_refresh_interval_seconds"]
    assert field.default == 60
