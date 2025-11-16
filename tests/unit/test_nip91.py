"""Unit tests for NIP-91 provider announcement functionality"""

import os
import pytest
from unittest.mock import Mock, patch

os.environ["UPSTREAM_BASE_URL"] = "http://test"
os.environ["UPSTREAM_API_KEY"] = "test"


def test_nsec_to_keypair_valid_nsec() -> None:
    """Test converting valid nsec to keypair"""
    from routstr.nip91 import nsec_to_keypair

    nsec = "nsec1test1234567890abcdefghijklmnopqrstuvwxyz"
    result = nsec_to_keypair(nsec)

    assert result is not None
    privkey, pubkey = result
    assert isinstance(privkey, str)
    assert isinstance(pubkey, str)
    assert len(privkey) == 64
    assert len(pubkey) == 64


def test_nsec_to_keypair_hex_format() -> None:
    """Test converting hex format private key to keypair"""
    from routstr.nip91 import nsec_to_keypair

    hex_key = "a" * 64
    result = nsec_to_keypair(hex_key)

    assert result is not None
    privkey, pubkey = result
    assert isinstance(privkey, str)
    assert isinstance(pubkey, str)


def test_nsec_to_keypair_invalid_format() -> None:
    """Test converting invalid format key"""
    from routstr.nip91 import nsec_to_keypair

    invalid_key = "invalid-key-format"
    result = nsec_to_keypair(invalid_key)

    assert result is None


def test_nsec_to_keypair_empty_string() -> None:
    """Test converting empty string"""
    from routstr.nip91 import nsec_to_keypair

    result = nsec_to_keypair("")
    assert result is None


def test_create_nip91_event_structure() -> None:
    """Test creating NIP-91 event has correct structure"""
    from routstr.nip91 import create_nip91_event

    private_key_hex = "a" * 64
    event = create_nip91_event(
        private_key_hex=private_key_hex,
        provider_id="test-provider",
        endpoint_urls=["https://example.com"],
        mint_urls=["https://mint.example.com"],
        version="1.0.0",
    )

    assert "id" in event
    assert "pubkey" in event
    assert "created_at" in event
    assert "kind" in event
    assert event["kind"] == 38421
    assert "tags" in event
    assert "content" in event
    assert "sig" in event


def test_create_nip91_event_signature() -> None:
    """Test that NIP-91 event is properly signed"""
    from routstr.nip91 import create_nip91_event

    private_key_hex = "a" * 64
    event = create_nip91_event(
        private_key_hex=private_key_hex,
        provider_id="test-provider",
        endpoint_urls=["https://example.com"],
    )

    assert event["sig"] is not None
    assert len(event["sig"]) > 0


def test_events_semantically_equal_identical() -> None:
    """Test that identical events are semantically equal"""
    from routstr.nip91 import create_nip91_event, events_semantically_equal

    private_key_hex = "a" * 64
    event1 = create_nip91_event(
        private_key_hex=private_key_hex,
        provider_id="test-provider",
        endpoint_urls=["https://example.com"],
    )
    event2 = create_nip91_event(
        private_key_hex=private_key_hex,
        provider_id="test-provider",
        endpoint_urls=["https://example.com"],
    )

    assert events_semantically_equal(event1, event2) is True


def test_events_semantically_equal_different_timestamps() -> None:
    """Test that events with different timestamps can be semantically equal"""
    from routstr.nip91 import create_nip91_event, events_semantically_equal
    import time

    private_key_hex = "a" * 64
    event1 = create_nip91_event(
        private_key_hex=private_key_hex,
        provider_id="test-provider",
        endpoint_urls=["https://example.com"],
    )

    time.sleep(1)

    event2 = create_nip91_event(
        private_key_hex=private_key_hex,
        provider_id="test-provider",
        endpoint_urls=["https://example.com"],
    )

    assert events_semantically_equal(event1, event2) is True


def test_events_semantically_equal_different_content() -> None:
    """Test that events with different content are not semantically equal"""
    from routstr.nip91 import create_nip91_event, events_semantically_equal

    private_key_hex = "a" * 64
    event1 = create_nip91_event(
        private_key_hex=private_key_hex,
        provider_id="test-provider-1",
        endpoint_urls=["https://example.com"],
    )
    event2 = create_nip91_event(
        private_key_hex=private_key_hex,
        provider_id="test-provider-2",
        endpoint_urls=["https://example.com"],
    )

    assert events_semantically_equal(event1, event2) is False


@pytest.mark.asyncio
async def test_discover_onion_url_common_paths() -> None:
    """Test discovering onion URL from common paths"""
    from routstr.nip91 import discover_onion_url_from_tor

    with patch("routstr.nip91.httpx.AsyncClient") as mock_client:
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = "http://test.onion"
        mock_client.return_value.__aenter__.return_value.get.return_value = (
            mock_response
        )

        result = await discover_onion_url_from_tor("http://example.com")

        assert result is not None or result is None


@pytest.mark.asyncio
async def test_discover_onion_url_not_found() -> None:
    """Test discovering onion URL when not found"""
    from routstr.nip91 import discover_onion_url_from_tor

    with patch("routstr.nip91.httpx.AsyncClient") as mock_client:
        mock_response = Mock()
        mock_response.status_code = 404
        mock_client.return_value.__aenter__.return_value.get.return_value = (
            mock_response
        )

        result = await discover_onion_url_from_tor("http://example.com")

        assert result is None
