import time
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_providers_uses_cache(async_client):
    from router import discovery

    discovery.CACHE_TTL = 3600
    discovery.PROVIDERS_CACHE = {
        "timestamp": time.time(),
        "providers": ["http://a.onion"],
    }

    with patch.object(discovery, "query_nostr_relay_with_search", AsyncMock()) as q, patch.object(
        discovery, "fetch_onion", AsyncMock()
    ) as f:
        response = await async_client.get("/v1/providers/")

        assert response.status_code == 200
        assert response.json()["providers"] == ["http://a.onion"]
        q.assert_not_called()
        f.assert_not_called()


@pytest.mark.asyncio
async def test_providers_queries_and_caches(async_client):
    from router import discovery

    discovery.CACHE_TTL = 3600
    discovery.PROVIDERS_CACHE = {"timestamp": 0, "providers": []}

    with patch.object(
        discovery,
        "query_nostr_relay_with_search",
        AsyncMock(return_value=[{"id": "1", "content": "http://b.onion"}]),
    ) as q, patch.object(
        discovery, "fetch_onion", AsyncMock(return_value={"status_code": 200, "json": {}})
    ) as f:
        response = await async_client.get("/v1/providers/")

        assert response.status_code == 200
        assert response.json()["providers"] == ["http://b.onion"]
        assert discovery.PROVIDERS_CACHE["providers"] == ["http://b.onion"]

        q.assert_called()
        f.assert_called()

        q.reset_mock()
        f.reset_mock()

        response2 = await async_client.get("/v1/providers/")

        assert response2.status_code == 200
        assert response2.json()["providers"] == ["http://b.onion"]
        q.assert_not_called()
        f.assert_not_called()

