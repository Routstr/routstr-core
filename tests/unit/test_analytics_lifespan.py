from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest

from routstr.core import main


@pytest.fixture
def isolated_lifespan(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    events: list[str] = []

    async def noop(*args: Any, **kwargs: Any) -> None:
        pass

    @asynccontextmanager
    async def sessions() -> Any:
        yield None

    async def initialize(*args: Any) -> Any:
        return main.global_settings

    async def background() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            events.append("background-stopped")

    monkeypatch.setattr(main, "configure_litellm", lambda: None)
    monkeypatch.setattr(main, "run_migrations", lambda: None)
    monkeypatch.setattr(main, "create_session", sessions)
    monkeypatch.setattr(main.SettingsService, "initialize", initialize)
    for name in (
        "import_sqlite_on_startup",
        "init_db",
        "bootstrap_secrets",
        "release_leaked_reservations_on_startup",
        "initialize_upstreams",
        "close_upstream_http_client",
    ):
        monkeypatch.setattr(main, name, noop)
    for name in (
        "update_prices_periodically",
        "update_sats_pricing",
        "periodic_payout",
        "announce_provider",
        "providers_cache_refresher",
        "periodic_stale_reservation_sweep",
        "periodic_dead_key_prune",
        "periodic_auto_topup",
        "periodic_refund_sweep",
        "periodic_refund_reconcile",
        "periodic_swap_reconcile",
        "periodic_routstr_fee_payout",
        "periodic_invoice_watcher",
        "refresh_model_maps_periodically",
        "sync_upstreams_periodically",
        "run_analytics",
    ):
        monkeypatch.setattr(main, name, background)
    import routstr.payment.price as price
    import routstr.upstream.helpers as helpers
    import routstr.upstream.model_paths as paths

    monkeypatch.setattr(price, "_update_prices", noop)
    monkeypatch.setattr(
        helpers, "refresh_upstreams_models_periodically", lambda *a: background()
    )
    monkeypatch.setattr(
        paths, "refresh_model_paths_periodically", lambda *a: background()
    )
    return events


@pytest.mark.asyncio
async def test_collector_start_failure_keeps_serving(
    isolated_lifespan: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    events = isolated_lifespan

    async def failed_prepare() -> None:
        raise RuntimeError("collector unavailable")

    async def shutdown(*, serving_stopped: bool) -> None:
        assert serving_stopped
        events.append("collector-stopped")

    monkeypatch.setattr(main, "prepare_analytics", failed_prepare)
    monkeypatch.setattr(main, "shutdown_analytics", shutdown)
    async with main.lifespan(main.app):
        events.append("serving")
    assert "serving" in events
    assert events[-1] == "collector-stopped"


@pytest.mark.asyncio
async def test_lifespan_stops_collection_after_serving_and_background_tasks(
    isolated_lifespan: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    events = isolated_lifespan

    async def prepare() -> None:
        events.append("collector-started")

    async def shutdown(*, serving_stopped: bool) -> None:
        assert serving_stopped
        assert events[0] == "collector-started"
        assert "serving-drained" in events
        assert "background-stopped" in events
        events.append("collector-stopped")

    monkeypatch.setattr(main, "prepare_analytics", prepare)
    monkeypatch.setattr(main, "shutdown_analytics", shutdown)
    async with main.lifespan(main.app):
        events.append("serving")
        await asyncio.sleep(0)
        # ASGI invokes lifespan shutdown after draining its request tasks.
        events.append("serving-drained")
    assert events[-1] == "collector-stopped"
