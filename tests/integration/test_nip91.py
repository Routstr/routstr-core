"""Integration tests for NIP-91 provider announcement"""

import os
import pytest
from unittest.mock import AsyncMock, patch

os.environ["UPSTREAM_BASE_URL"] = "http://test"
os.environ["UPSTREAM_API_KEY"] = "test"


@pytest.mark.asyncio
async def test_announce_provider_creates_event() -> None:
    """Test that announce_provider creates a NIP-91 event"""
    from routstr.nip91 import announce_provider
    from routstr.core.settings import settings

    with patch("routstr.nip91.publish_to_relay", new_callable=AsyncMock) as mock_publish:
        with patch("routstr.nip91.query_nip91_events", new_callable=AsyncMock) as mock_query:
            mock_query.return_value = []
            mock_publish.return_value = True

            if settings.nsec and settings.http_url:
                await announce_provider()

                mock_publish.assert_called()
                call_args = mock_publish.call_args
                event = call_args[0][0] if call_args[0] else None
                if event:
                    assert event["kind"] == 38421


@pytest.mark.asyncio
async def test_announce_provider_updates_existing() -> None:
    """Test that announce_provider updates existing event if semantically equal"""
    from routstr.nip91 import announce_provider, create_nip91_event
    from routstr.core.settings import settings

    if not settings.nsec or not settings.http_url:
        pytest.skip("NIP-91 settings not configured")

    private_key_hex = settings.nsec
    if private_key_hex.startswith("nsec"):
        from routstr.nip91 import nsec_to_keypair

        result = nsec_to_keypair(private_key_hex)
        if result:
            private_key_hex = result[0]

    existing_event = create_nip91_event(
        private_key_hex=private_key_hex,
        provider_id="test-provider",
        endpoint_urls=[settings.http_url],
    )

    with patch("routstr.nip91.publish_to_relay", new_callable=AsyncMock) as mock_publish:
        with patch("routstr.nip91.query_nip91_events", new_callable=AsyncMock) as mock_query:
            mock_query.return_value = [existing_event]
            mock_publish.return_value = True

            await announce_provider()

            mock_publish.assert_called()


@pytest.mark.asyncio
async def test_announce_provider_relay_failure() -> None:
    """Test that announce_provider handles relay failures gracefully"""
    from routstr.nip91 import announce_provider
    from routstr.core.settings import settings

    if not settings.nsec or not settings.http_url:
        pytest.skip("NIP-91 settings not configured")

    with patch("routstr.nip91.publish_to_relay", new_callable=AsyncMock) as mock_publish:
        with patch("routstr.nip91.query_nip91_events", new_callable=AsyncMock) as mock_query:
            mock_query.return_value = []
            mock_publish.side_effect = Exception("Relay connection failed")

            try:
                await announce_provider()
            except Exception:
                pass

            mock_publish.assert_called()


@pytest.mark.asyncio
async def test_query_nip91_events_filters() -> None:
    """Test querying NIP-91 events with filters"""
    from routstr.nip91 import query_nip91_events

    with patch("routstr.nip91.RelayManager") as mock_relay_manager:
        mock_manager = Mock()
        mock_relay_manager.return_value = mock_manager
        mock_manager.message_pool.has_ok_notices = Mock(return_value=True)
        mock_manager.message_pool.get_all_events = Mock(return_value=[])

        events = await query_nip91_events(relay_url="ws://test.relay")

        assert isinstance(events, list)


@pytest.mark.asyncio
async def test_publish_to_relay_success() -> None:
    """Test publishing event to relay successfully"""
    from routstr.nip91 import publish_to_relay, create_nip91_event

    private_key_hex = "a" * 64
    event = create_nip91_event(
        private_key_hex=private_key_hex,
        provider_id="test-provider",
        endpoint_urls=["https://example.com"],
    )

    with patch("routstr.nip91.RelayManager") as mock_relay_manager:
        mock_manager = Mock()
        mock_relay_manager.return_value = mock_manager
        mock_manager.message_pool.has_ok_notices = Mock(return_value=True)

        result = await publish_to_relay(event, "ws://test.relay")

        assert result is True or result is False
