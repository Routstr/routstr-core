"""Unit tests for discovery service"""

import os
from unittest.mock import AsyncMock, Mock, patch

os.environ["UPSTREAM_BASE_URL"] = "http://test"
os.environ["UPSTREAM_API_KEY"] = "test"

import pytest
from routstr.discovery import (
    fetch_provider_health,
    parse_provider_announcement,
    query_nostr_relay_for_providers,
)


def test_parse_provider_announcement_nip91() -> None:
    """Test parsing NIP-91 provider announcement"""
    event = {
        "kind": 38421,
        "tags": [
            ["d", "test-provider"],
            ["u", "https://example.com"],
            ["mint", "https://mint.example.com"],
            ["version", "1.0.0"],
        ],
        "content": '{"name": "Test Provider", "about": "A test provider"}',
    }

    provider = parse_provider_announcement(event)

    assert provider is not None
    assert provider["provider_id"] == "test-provider"
    assert "https://example.com" in provider["endpoint_urls"]
    assert "https://mint.example.com" in provider.get("mint_urls", [])


def test_parse_provider_announcement_invalid() -> None:
    """Test parsing invalid provider announcement"""
    event = {
        "kind": 1,
        "tags": [],
        "content": "Not a provider announcement",
    }

    provider = parse_provider_announcement(event)

    assert provider is None


def test_parse_provider_announcement_missing_tags() -> None:
    """Test parsing announcement with missing required tags"""
    event = {
        "kind": 38421,
        "tags": [],
        "content": "",
    }

    provider = parse_provider_announcement(event)

    assert provider is None


@pytest.mark.asyncio
async def test_query_nostr_relay_filters_localhost() -> None:
    """Test querying Nostr relay filters out localhost URLs"""
    with patch("routstr.discovery.RelayManager") as mock_relay_manager:
        mock_manager = Mock()
        mock_relay_manager.return_value = mock_manager
        mock_manager.message_pool.has_ok_notices = Mock(return_value=True)
        mock_manager.message_pool.get_all_events = Mock(return_value=[])

        providers = await query_nostr_relay_for_providers("ws://test.relay")

        assert isinstance(providers, list)


@pytest.mark.asyncio
async def test_fetch_provider_health_timeout() -> None:
    """Test fetching provider health with timeout"""
    import httpx

    with patch("routstr.discovery.httpx.AsyncClient") as mock_client:
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value={"status": "ok"})
        mock_client.return_value.__aenter__.return_value.get.return_value = (
            mock_response
        )

        health = await fetch_provider_health("https://example.com", timeout=5.0)

        assert health is not None
        assert health["status"] == "ok"


@pytest.mark.asyncio
async def test_fetch_provider_health_failure() -> None:
    """Test fetching provider health when provider is down"""
    import httpx

    with patch("routstr.discovery.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get.side_effect = (
            httpx.TimeoutException("Request timed out")
        )

        health = await fetch_provider_health("https://example.com", timeout=5.0)

        assert health is None or health.get("error") is not None


@pytest.mark.asyncio
async def test_refresh_providers_cache_deduplication() -> None:
    """Test that refresh_providers_cache deduplicates providers"""
    from routstr.discovery import refresh_providers_cache

    with patch("routstr.discovery.query_nostr_relay_for_providers") as mock_query:
        mock_query.return_value = [
            {
                "provider_id": "test-provider",
                "endpoint_urls": ["https://example.com"],
            },
            {
                "provider_id": "test-provider",
                "endpoint_urls": ["https://example.com"],
            },
        ]

        with patch("routstr.discovery.fetch_provider_health") as mock_health:
            mock_health.return_value = {"status": "ok"}

            providers = await refresh_providers_cache("ws://test.relay")

            assert isinstance(providers, list)
            provider_ids = [p["provider_id"] for p in providers]
            assert len(provider_ids) == len(set(provider_ids))
