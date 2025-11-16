"""Integration tests for NIP-91 provider announcement."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.integration
@pytest.mark.asyncio
async def test_announce_provider_creates_event() -> None:
    """Test that announce_provider creates a NIP-91 event."""
    from routstr.nip91 import announce_provider, create_nip91_event
    
    private_key_hex = "a" * 64
    provider_id = "test-provider"
    endpoint_urls = ["https://example.com"]
    
    with patch("routstr.nip91.publish_to_relay", return_value=True):
        with patch("routstr.nip91.query_nip91_events", return_value=([], True)):
            result = await announce_provider()
            
            assert result is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_announce_provider_updates_existing() -> None:
    """Test that announce_provider updates existing event."""
    from routstr.nip91 import announce_provider
    
    existing_event = {
        "id": "existing_id",
        "kind": 38421,
        "created_at": 1234567890,
        "tags": [["d", "test-provider"], ["u", "https://old.example.com"]],
        "content": "",
    }
    
    with patch("routstr.nip91.query_nip91_events", return_value=([existing_event], True)):
        with patch("routstr.nip91.publish_to_relay", return_value=True):
            result = await announce_provider()
            
            assert result is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_announce_provider_relay_failure() -> None:
    """Test announce_provider handles relay failure gracefully."""
    from routstr.nip91 import announce_provider
    
    with patch("routstr.nip91.publish_to_relay", return_value=False):
        with patch("routstr.nip91.query_nip91_events", return_value=([], True)):
            result = await announce_provider()
            
            assert result is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_query_nip91_events_filters() -> None:
    """Test querying NIP-91 events with filters."""
    from routstr.nip91 import query_nip91_events
    
    private_key_hex = "a" * 64
    
    with patch("routstr.nip91.RelayManager") as mock_rm_class:
        mock_rm = MagicMock()
        mock_rm_class.return_value = mock_rm
        
        events, ok = await query_nip91_events(
            relay_url="ws://test-relay.com",
            pubkey=private_key_hex,
            provider_id="test-provider",
        )
        
        assert isinstance(events, list)
        assert isinstance(ok, bool)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_publish_to_relay_success() -> None:
    """Test successful publishing to relay."""
    from routstr.nip91 import create_nip91_event, publish_to_relay
    
    private_key_hex = "a" * 64
    event = create_nip91_event(
        private_key_hex=private_key_hex,
        provider_id="test-provider",
        endpoint_urls=["https://example.com"],
    )
    
    with patch("routstr.nip91.RelayManager") as mock_rm_class:
        mock_rm = MagicMock()
        mock_rm_class.return_value = mock_rm
        
        result = await publish_to_relay("ws://test-relay.com", event)
        
        assert isinstance(result, bool)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_publish_to_relay_timeout() -> None:
    """Test publishing to relay with timeout."""
    from routstr.nip91 import create_nip91_event, publish_to_relay
    
    private_key_hex = "a" * 64
    event = create_nip91_event(
        private_key_hex=private_key_hex,
        provider_id="test-provider",
        endpoint_urls=["https://example.com"],
    )
    
    with patch("routstr.nip91.RelayManager") as mock_rm_class:
        mock_rm = MagicMock()
        mock_rm.open_connections.side_effect = Exception("Connection timeout")
        mock_rm_class.return_value = mock_rm
        
        result = await publish_to_relay("ws://test-relay.com", event, timeout=5)
        
        assert result is False


def test_discover_onion_url_common_paths() -> None:
    """Test discovering onion URL from common paths."""
    from routstr.nip91 import discover_onion_url_from_tor
    import tempfile
    import os
    
    with tempfile.TemporaryDirectory() as tmpdir:
        hostname_file = os.path.join(tmpdir, "hs", "router", "hostname")
        os.makedirs(os.path.dirname(hostname_file), exist_ok=True)
        
        with open(hostname_file, "w") as f:
            f.write("test1234567890abcdef.onion\n")
        
        result = discover_onion_url_from_tor(base_dir=tmpdir)
        
        assert result is not None
        assert result.startswith("http://")
        assert result.endswith(".onion")


def test_discover_onion_url_not_found() -> None:
    """Test discovering onion URL when not found."""
    from routstr.nip91 import discover_onion_url_from_tor
    import tempfile
    
    with tempfile.TemporaryDirectory() as tmpdir:
        result = discover_onion_url_from_tor(base_dir=tmpdir)
        
        assert result is None
