"""Cache behavior of the admin provider catalog listing."""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from routstr.core import admin
from routstr.core.admin import _get_remote_models, invalidate_remote_models_cache

PROVIDER = SimpleNamespace(provider_type="generic")


def _upstream(fetch: Any) -> Any:
    return patch(
        "routstr.upstream.helpers._instantiate_provider",
        return_value=SimpleNamespace(fetch_models=fetch),
    )


@pytest.mark.asyncio
async def test_second_read_is_served_from_cache() -> None:
    fetch = AsyncMock(return_value=["a"])
    with _upstream(fetch):
        assert await _get_remote_models(PROVIDER, 1) == ["a"]  # type: ignore[arg-type]
        assert await _get_remote_models(PROVIDER, 1) == ["a"]  # type: ignore[arg-type]
    assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_force_refresh_and_invalidation_refetch() -> None:
    fetch = AsyncMock(side_effect=[["a"], ["b"], ["c"]])
    with _upstream(fetch):
        await _get_remote_models(PROVIDER, 1)  # type: ignore[arg-type]
        assert await _get_remote_models(PROVIDER, 1, force_refresh=True) == ["b"]  # type: ignore[arg-type]
        invalidate_remote_models_cache(1)
        assert await _get_remote_models(PROVIDER, 1) == ["c"]  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_expired_entry_is_refetched() -> None:
    fetch = AsyncMock(side_effect=[["a"], ["b"]])
    with _upstream(fetch):
        await _get_remote_models(PROVIDER, 1)  # type: ignore[arg-type]
        stamp, models = admin._remote_models_cache[1]
        admin._remote_models_cache[1] = (
            stamp - admin._REMOTE_MODELS_TTL_SECONDS - 1,
            models,
        )
        assert await _get_remote_models(PROVIDER, 1) == ["b"]  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_failed_refresh_falls_back_to_stale_listing() -> None:
    fetch = AsyncMock(side_effect=[["a"], RuntimeError("upstream down")])
    with _upstream(fetch):
        await _get_remote_models(PROVIDER, 1)  # type: ignore[arg-type]
        assert await _get_remote_models(PROVIDER, 1, force_refresh=True) == ["a"]  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_failed_first_fetch_returns_empty_and_caches_nothing() -> None:
    fetch = AsyncMock(side_effect=RuntimeError("upstream down"))
    with _upstream(fetch):
        assert await _get_remote_models(PROVIDER, 1) == []  # type: ignore[arg-type]
    assert 1 not in admin._remote_models_cache


@pytest.mark.asyncio
async def test_concurrent_readers_share_one_fetch() -> None:
    release = asyncio.Event()

    async def slow_fetch() -> list[str]:
        await release.wait()
        return ["a"]

    fetch = AsyncMock(side_effect=slow_fetch)
    with _upstream(fetch):
        readers = [
            asyncio.create_task(_get_remote_models(PROVIDER, 1))  # type: ignore[arg-type]
            for _ in range(5)
        ]
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(*readers)
    assert results == [["a"]] * 5
    assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_invalidation_during_fetch_discards_in_flight_result() -> None:
    release = asyncio.Event()

    async def slow_fetch() -> list[str]:
        await release.wait()
        return ["old"]

    with _upstream(AsyncMock(side_effect=slow_fetch)):
        reader = asyncio.create_task(_get_remote_models(PROVIDER, 1))  # type: ignore[arg-type]
        await asyncio.sleep(0)
        invalidate_remote_models_cache(1)
        release.set()
        assert await reader == ["old"]
    assert 1 not in admin._remote_models_cache


@pytest.mark.asyncio
async def test_uninstantiable_provider_returns_empty() -> None:
    with patch("routstr.upstream.helpers._instantiate_provider", return_value=None):
        assert await _get_remote_models(PROVIDER, 1) == []  # type: ignore[arg-type]
