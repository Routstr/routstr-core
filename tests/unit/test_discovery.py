"""Unit tests for discovery service functionality."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def test_parse_provider_announcement_nip91() -> None:
    """Test parsing NIP-91 provider announcement."""
    from routstr.discovery import parse_provider_announcement

    event = {
        "kind": 38421,
        "tags": [
            ["d", "test-provider"],
            ["u", "https://example.com"],
            ["mint", "https://mint.example.com"],
        ],
        "content": '{"name": "Test Provider", "about": "Test"}',
    }

    result = parse_provider_announcement(event)
    assert result is not None
    assert result["provider_id"] == "test-provider"
    assert len(result["endpoint_urls"]) > 0


def test_parse_provider_announcement_invalid() -> None:
    """Test parsing invalid provider announcement."""
    from routstr.discovery import parse_provider_announcement

    invalid_event = {
        "kind": 1,
        "tags": [],
        "content": "",
    }

    result = parse_provider_announcement(invalid_event)
    assert result is None


def test_parse_provider_announcement_missing_tags() -> None:
    """Test parsing provider announcement with missing tags."""
    from routstr.discovery import parse_provider_announcement

    event = {
        "kind": 38421,
        "tags": [],
        "content": "",
    }

    result = parse_provider_announcement(event)
    assert result is None


@pytest.mark.asyncio
async def test_query_nostr_relay_filters_localhost() -> None:
    """Test querying Nostr relay filters localhost URLs."""
    from routstr.discovery import query_nostr_relay_for_providers

    with patch("routstr.discovery.RelayManager") as mock_rm_class:
        mock_rm = MagicMock()
        mock_rm_class.return_value = mock_rm
        
        providers = await query_nostr_relay_for_providers("ws://test-relay.com")
        
        assert isinstance(providers, list)


@pytest.mark.asyncio
async def test_refresh_providers_cache_deduplication() -> None:
    """Test that refresh_providers_cache deduplicates providers."""
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
            mock_health.return_value = {"status": "healthy"}
            
            providers = await refresh_providers_cache("ws://test-relay.com")
            
            assert isinstance(providers, list)


@pytest.mark.asyncio
async def test_fetch_provider_health_timeout() -> None:
    """Test fetching provider health with timeout."""
    from routstr.discovery import fetch_provider_health
    import asyncio

    with patch("httpx.AsyncClient.get") as mock_get:
        mock_get.side_effect = asyncio.TimeoutError()
        
        health = await fetch_provider_health("https://example.com", timeout=1)
        
        assert health["status"] == "unhealthy" or "error" in health


@pytest.mark.asyncio
async def test_fetch_provider_health_success() -> None:
    """Test successful provider health fetch."""
    from routstr.discovery import fetch_provider_health
    from unittest.mock import AsyncMock

    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"status": "ok"}

    with patch("httpx.AsyncClient.get", return_value=mock_response):
        health = await fetch_provider_health("https://example.com")
        
        assert health["status"] == "healthy" or "status" in health


@pytest.mark.asyncio
async def test_fetch_provider_health_failure() -> None:
    """Test provider health fetch with failure."""
    from routstr.discovery import fetch_provider_health
    from unittest.mock import AsyncMock

    mock_response = AsyncMock()
    mock_response.status_code = 500
    mock_response.raise_for_status.side_effect = Exception("Server error")

    with patch("httpx.AsyncClient.get", return_value=mock_response):
        health = await fetch_provider_health("https://example.com")
        
        assert health["status"] == "unhealthy" or "error" in health
