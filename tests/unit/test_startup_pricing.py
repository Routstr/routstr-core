"""Startup must not expose unpriced or stale provider snapshots."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Request, Response

from routstr import proxy
from routstr.algorithm import create_model_mappings
from routstr.core.main import _bootstrap_providers_and_pricing
from routstr.payment import price
from routstr.payment.cost_calculation import CostData, calculate_cost
from routstr.payment.models import (
    Architecture,
    Model,
    Pricing,
    _update_sats_pricing_once,
    models,
)
from routstr.upstream.generic import GenericUpstreamProvider


@pytest.fixture(autouse=True)
def isolate_provider_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proxy, "_upstreams", [])
    monkeypatch.setattr(proxy, "_pending_upstreams", None)
    monkeypatch.setattr(proxy, "_provider_map", {})
    monkeypatch.setattr(proxy, "_unique_models", {})
    monkeypatch.setattr(proxy, "_warming_up", False)
    monkeypatch.setattr(proxy, "_upstreams_initialization_lock", asyncio.Lock())
    monkeypatch.setattr(price, "SATS_USD_PRICE", None)

    async def refresh_maps() -> None:
        _, proxy._provider_map, proxy._unique_models = create_model_mappings(
            proxy.get_upstreams(), {}, set()
        )

    monkeypatch.setattr(proxy, "refresh_model_maps", refresh_maps)


def unpriced_provider() -> GenericUpstreamProvider:
    provider = GenericUpstreamProvider("http://upstream.test/v1", provider_fee=1.0)
    provider._models_cache = [
        Model(
            id="startup-model",
            name="Startup Model",
            created=0,
            description="",
            context_length=128_000,
            architecture=Architecture(
                modality="text",
                input_modalities=["text"],
                output_modalities=["text"],
                tokenizer="unknown",
                instruct_type=None,
            ),
            pricing=Pricing(prompt=0.000001, completion=0.000002),
        )
    ]
    return provider


@pytest.mark.asyncio
async def test_bootstrap_waits_for_initial_price_before_publishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    price_started = asyncio.Event()
    release_price = asyncio.Event()

    async def delayed_price() -> None:
        price_started.set()
        await release_price.wait()
        monkeypatch.setattr(price, "SATS_USD_PRICE", 0.001)

    monkeypatch.setattr(price, "_update_prices", delayed_price)
    monkeypatch.setattr(proxy, "init_upstreams", AsyncMock(return_value=[unpriced_provider()]))
    proxy.mark_warming_up()
    bootstrap = asyncio.create_task(_bootstrap_providers_and_pricing())
    try:
        await asyncio.wait_for(price_started.wait(), timeout=2)
        # The mock provider load and map refresh have no I/O; yield to any runnable
        # bootstrap work while the BTC quote remains deterministically blocked.
        await asyncio.sleep(0)
        assert proxy.get_upstreams() == []
        assert proxy.get_candidates("startup-model") is None
        assert proxy.is_warming_up()
        response = await models(Request({"type": "http"}), MagicMock())
        assert isinstance(response, Response)
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "2"
    finally:
        release_price.set()
        await asyncio.wait_for(bootstrap, timeout=2)

    candidates = proxy.get_candidates("startup-model")
    assert candidates
    model, provider = candidates[0]
    assert model.sats_pricing is not None
    assert provider.get_cached_model_by_id(model.id) is model
    assert not proxy.is_warming_up()
    cost = await calculate_cost(
        {"model": model.id, "usage": {"prompt_tokens": 100, "completion_tokens": 50}},
        max_cost=1000,
        model_obj=model,
        provider_fee=1.0,
    )
    assert isinstance(cost, CostData)
    assert cost.total_msats == 200


@pytest.mark.asyncio
async def test_bootstrap_without_initial_price_recovers_on_periodic_pricing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(price, "_update_prices", AsyncMock())
    monkeypatch.setattr(proxy, "init_upstreams", AsyncMock(return_value=[unpriced_provider()]))
    proxy.mark_warming_up()

    await _bootstrap_providers_and_pricing()

    assert proxy.get_upstreams() == []
    assert proxy.get_candidates("startup-model") is None
    assert not proxy.is_warming_up()

    with pytest.raises(ValueError, match="SATS price not initialized"):
        await _update_sats_pricing_once()
    assert proxy.get_upstreams() == []
    assert proxy.get_candidates("startup-model") is None

    monkeypatch.setattr(price, "SATS_USD_PRICE", 0.001)
    await _update_sats_pricing_once()

    candidates = proxy.get_candidates("startup-model")
    assert candidates
    model, provider = candidates[0]
    assert proxy.get_upstreams() == [provider]
    assert model.sats_pricing is not None
    assert model.sats_pricing.prompt == pytest.approx(0.001)
    assert provider.get_cached_model_by_id(model.id) is model
    response = await models(Request({"type": "http"}), MagicMock())
    assert isinstance(response, dict)
    assert len(response["data"]) == 1
    assert response["data"][0]["id"] == model.id
    assert response["data"][0]["sats_pricing"] == model.sats_pricing.dict()


@pytest.mark.asyncio
async def test_admin_disable_cannot_be_overwritten_by_startup_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(price, "SATS_USD_PRICE", 0.001)
    stale_snapshot_loaded = asyncio.Event()
    release_startup = asyncio.Event()
    admin_started = asyncio.Event()
    provider = unpriced_provider()
    enabled = True
    load_count = 0

    async def load_snapshot() -> list[GenericUpstreamProvider]:
        nonlocal load_count
        load_count += 1
        snapshot = [provider] if enabled else []
        if load_count == 1:
            stale_snapshot_loaded.set()
            await release_startup.wait()
        return snapshot

    async def admin_disable() -> None:
        nonlocal enabled
        enabled = False  # The committed admin database update precedes reinit.
        admin_started.set()
        await proxy.reinitialize_upstreams()

    monkeypatch.setattr(proxy, "init_upstreams", load_snapshot)
    startup = asyncio.create_task(proxy.initialize_upstreams())
    await asyncio.wait_for(stale_snapshot_loaded.wait(), timeout=2)
    admin = asyncio.create_task(admin_disable())
    try:
        await asyncio.wait_for(admin_started.wait(), timeout=2)
        await asyncio.sleep(0)
    finally:
        release_startup.set()
        await asyncio.wait_for(asyncio.gather(startup, admin), timeout=2)

    assert load_count == 2
    assert proxy.get_upstreams() == []
    assert proxy.get_candidates("startup-model") is None
    assert proxy.get_unique_models() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reload_fails", [False, True])
async def test_periodic_recovery_cannot_restore_pending_provider_after_admin_disable(
    monkeypatch: pytest.MonkeyPatch, reload_fails: bool
) -> None:
    monkeypatch.setattr(price, "_update_prices", AsyncMock())
    monkeypatch.setattr(proxy, "init_upstreams", AsyncMock(return_value=[unpriced_provider()]))
    await _bootstrap_providers_and_pricing()
    assert proxy.get_upstreams() == []

    admin_loading = asyncio.Event()
    release_admin = asyncio.Event()
    recovery_started = asyncio.Event()

    async def load_disabled_snapshot() -> list[GenericUpstreamProvider]:
        admin_loading.set()
        await release_admin.wait()
        if reload_fails:
            raise RuntimeError("admin reload failed after committed disable")
        return []

    async def recover_prices() -> None:
        recovery_started.set()
        await _update_sats_pricing_once()

    monkeypatch.setattr(proxy, "init_upstreams", load_disabled_snapshot)
    admin = asyncio.create_task(proxy.reinitialize_upstreams())
    await asyncio.wait_for(admin_loading.wait(), timeout=2)
    monkeypatch.setattr(price, "SATS_USD_PRICE", 0.001)
    recovery = asyncio.create_task(recover_prices())
    try:
        await asyncio.wait_for(recovery_started.wait(), timeout=2)
        assert proxy.get_upstreams() == []
        assert proxy.get_candidates("startup-model") is None
    finally:
        release_admin.set()
        if reload_fails:
            with pytest.raises(RuntimeError, match="admin reload failed"):
                await asyncio.wait_for(admin, timeout=2)
        else:
            await asyncio.wait_for(admin, timeout=2)
        await asyncio.wait_for(recovery, timeout=2)

    # A later tick must not revive the old pending snapshot either.
    await _update_sats_pricing_once()
    assert proxy.get_upstreams() == []
    assert proxy.get_candidates("startup-model") is None
    assert proxy.get_unique_models() == []
