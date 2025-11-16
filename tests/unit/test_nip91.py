"""Unit tests for NIP-91 provider announcement functionality."""

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from routstr.nip91 import (
    create_nip91_event,
    discover_onion_url_from_tor,
    events_semantically_equal,
    nsec_to_keypair,
)


@pytest.mark.asyncio
async def test_nsec_to_keypair_valid_nsec() -> None:
    """Test converting a valid nsec private key to keypair."""
    test_nsec = "nsec1vl029mgpspedva04g90vltkh6fvh240zqtv9k0t9af8935ke9laqsnlfe5"
    
    result = nsec_to_keypair(test_nsec)
    
    assert result is not None
    privkey_hex, pubkey_hex = result
    assert len(privkey_hex) == 64
    assert len(pubkey_hex) == 64
    assert all(c in "0123456789abcdef" for c in privkey_hex)
    assert all(c in "0123456789abcdef" for c in pubkey_hex)


@pytest.mark.asyncio
async def test_nsec_to_keypair_hex_format() -> None:
    """Test converting a hex private key to keypair."""
    test_hex = "67dab8473d4c2be1f598a92e8d3c13e6f4f8d0e2b5c6a7b8c9d0e1f2a3b4c5d6"
    
    result = nsec_to_keypair(test_hex)
    
    assert result is not None
    privkey_hex, pubkey_hex = result
    assert len(privkey_hex) == 64
    assert len(pubkey_hex) == 64


@pytest.mark.asyncio
async def test_nsec_to_keypair_invalid_format() -> None:
    """Test that invalid format returns None."""
    result = nsec_to_keypair("invalid_key_format")
    assert result is None


@pytest.mark.asyncio
async def test_nsec_to_keypair_empty_string() -> None:
    """Test that empty string returns None."""
    result = nsec_to_keypair("")
    assert result is None


@pytest.mark.asyncio
async def test_nsec_to_keypair_wrong_length() -> None:
    """Test that hex key with wrong length returns None."""
    result = nsec_to_keypair("abcd1234")
    assert result is None


@pytest.mark.asyncio
async def test_create_nip91_event_structure() -> None:
    """Test that created NIP-91 event has correct structure."""
    test_privkey = "67dab8473d4c2be1f598a92e8d3c13e6f4f8d0e2b5c6a7b8c9d0e1f2a3b4c5d6"
    provider_id = "test-provider"
    endpoint_urls = ["https://api.test.com/v1"]
    mint_urls = ["https://mint.test.com"]
    
    event = create_nip91_event(
        private_key_hex=test_privkey,
        provider_id=provider_id,
        endpoint_urls=endpoint_urls,
        mint_urls=mint_urls,
        version="1.0.0",
        metadata={"name": "Test Provider", "about": "Test description"},
    )
    
    assert isinstance(event, dict)
    assert "id" in event
    assert "pubkey" in event
    assert "created_at" in event
    assert "kind" in event
    assert event["kind"] == 38421
    assert "tags" in event
    assert "content" in event
    assert "sig" in event
    
    tags = event["tags"]
    assert any(tag[0] == "d" and tag[1] == provider_id for tag in tags if len(tag) >= 2)
    assert any(tag[0] == "u" and tag[1] in endpoint_urls for tag in tags if len(tag) >= 2)
    assert any(tag[0] == "mint" and tag[1] in mint_urls for tag in tags if len(tag) >= 2)
    assert any(tag[0] == "version" and tag[1] == "1.0.0" for tag in tags if len(tag) >= 2)


@pytest.mark.asyncio
async def test_create_nip91_event_without_optional_fields() -> None:
    """Test creating NIP-91 event without optional fields."""
    test_privkey = "67dab8473d4c2be1f598a92e8d3c13e6f4f8d0e2b5c6a7b8c9d0e1f2a3b4c5d6"
    
    event = create_nip91_event(
        private_key_hex=test_privkey,
        provider_id="minimal-provider",
        endpoint_urls=["https://api.test.com/v1"],
    )
    
    assert event["kind"] == 38421
    assert "id" in event
    assert "sig" in event
    tags = event["tags"]
    assert any(tag[0] == "d" for tag in tags)
    assert any(tag[0] == "u" for tag in tags)


@pytest.mark.asyncio
async def test_create_nip91_event_signature() -> None:
    """Test that created event has valid signature."""
    test_privkey = "67dab8473d4c2be1f598a92e8d3c13e6f4f8d0e2b5c6a7b8c9d0e1f2a3b4c5d6"
    
    event = create_nip91_event(
        private_key_hex=test_privkey,
        provider_id="test",
        endpoint_urls=["https://test.com"],
    )
    
    assert len(event["sig"]) == 128


@pytest.mark.asyncio
async def test_create_nip91_event_metadata_serialization() -> None:
    """Test that metadata is properly serialized to JSON."""
    test_privkey = "67dab8473d4c2be1f598a92e8d3c13e6f4f8d0e2b5c6a7b8c9d0e1f2a3b4c5d6"
    metadata = {"name": "Test", "about": "Description", "picture": "https://example.com/pic.jpg"}
    
    event = create_nip91_event(
        private_key_hex=test_privkey,
        provider_id="test",
        endpoint_urls=["https://test.com"],
        metadata=metadata,
    )
    
    parsed_content = json.loads(event["content"])
    assert parsed_content == metadata


@pytest.mark.asyncio
async def test_events_semantically_equal_identical() -> None:
    """Test that identical events are semantically equal."""
    test_privkey = "67dab8473d4c2be1f598a92e8d3c13e6f4f8d0e2b5c6a7b8c9d0e1f2a3b4c5d6"
    
    event1 = create_nip91_event(
        private_key_hex=test_privkey,
        provider_id="test-provider",
        endpoint_urls=["https://api.test.com/v1"],
        mint_urls=["https://mint.test.com"],
        version="1.0.0",
        metadata={"name": "Test"},
    )
    
    event2 = create_nip91_event(
        private_key_hex=test_privkey,
        provider_id="test-provider",
        endpoint_urls=["https://api.test.com/v1"],
        mint_urls=["https://mint.test.com"],
        version="1.0.0",
        metadata={"name": "Test"},
    )
    
    assert events_semantically_equal(event1, event2)


@pytest.mark.asyncio
async def test_events_semantically_equal_different_timestamps() -> None:
    """Test that events with different timestamps but same content are equal."""
    test_privkey = "67dab8473d4c2be1f598a92e8d3c13e6f4f8d0e2b5c6a7b8c9d0e1f2a3b4c5d6"
    
    event1 = create_nip91_event(
        private_key_hex=test_privkey,
        provider_id="test",
        endpoint_urls=["https://test.com"],
    )
    
    import time
    time.sleep(0.01)
    
    event2 = create_nip91_event(
        private_key_hex=test_privkey,
        provider_id="test",
        endpoint_urls=["https://test.com"],
    )
    
    assert events_semantically_equal(event1, event2)


@pytest.mark.asyncio
async def test_events_semantically_equal_different_content() -> None:
    """Test that events with different content are not equal."""
    test_privkey = "67dab8473d4c2be1f598a92e8d3c13e6f4f8d0e2b5c6a7b8c9d0e1f2a3b4c5d6"
    
    event1 = create_nip91_event(
        private_key_hex=test_privkey,
        provider_id="test",
        endpoint_urls=["https://test.com"],
        metadata={"name": "Provider 1"},
    )
    
    event2 = create_nip91_event(
        private_key_hex=test_privkey,
        provider_id="test",
        endpoint_urls=["https://test.com"],
        metadata={"name": "Provider 2"},
    )
    
    assert not events_semantically_equal(event1, event2)


@pytest.mark.asyncio
async def test_events_semantically_equal_different_urls() -> None:
    """Test that events with different URLs are not equal."""
    test_privkey = "67dab8473d4c2be1f598a92e8d3c13e6f4f8d0e2b5c6a7b8c9d0e1f2a3b4c5d6"
    
    event1 = create_nip91_event(
        private_key_hex=test_privkey,
        provider_id="test",
        endpoint_urls=["https://test1.com"],
    )
    
    event2 = create_nip91_event(
        private_key_hex=test_privkey,
        provider_id="test",
        endpoint_urls=["https://test2.com"],
    )
    
    assert not events_semantically_equal(event1, event2)


@pytest.mark.asyncio
async def test_events_semantically_equal_different_provider_id() -> None:
    """Test that events with different provider IDs are not equal."""
    test_privkey = "67dab8473d4c2be1f598a92e8d3c13e6f4f8d0e2b5c6a7b8c9d0e1f2a3b4c5d6"
    
    event1 = create_nip91_event(
        private_key_hex=test_privkey,
        provider_id="provider-1",
        endpoint_urls=["https://test.com"],
    )
    
    event2 = create_nip91_event(
        private_key_hex=test_privkey,
        provider_id="provider-2",
        endpoint_urls=["https://test.com"],
    )
    
    assert not events_semantically_equal(event1, event2)


@pytest.mark.asyncio
async def test_events_semantically_equal_different_version() -> None:
    """Test that events with different versions are not equal."""
    test_privkey = "67dab8473d4c2be1f598a92e8d3c13e6f4f8d0e2b5c6a7b8c9d0e1f2a3b4c5d6"
    
    event1 = create_nip91_event(
        private_key_hex=test_privkey,
        provider_id="test",
        endpoint_urls=["https://test.com"],
        version="1.0.0",
    )
    
    event2 = create_nip91_event(
        private_key_hex=test_privkey,
        provider_id="test",
        endpoint_urls=["https://test.com"],
        version="2.0.0",
    )
    
    assert not events_semantically_equal(event1, event2)


@pytest.mark.asyncio
async def test_events_semantically_equal_different_mints() -> None:
    """Test that events with different mint URLs are not equal."""
    test_privkey = "67dab8473d4c2be1f598a92e8d3c13e6f4f8d0e2b5c6a7b8c9d0e1f2a3b4c5d6"
    
    event1 = create_nip91_event(
        private_key_hex=test_privkey,
        provider_id="test",
        endpoint_urls=["https://test.com"],
        mint_urls=["https://mint1.com"],
    )
    
    event2 = create_nip91_event(
        private_key_hex=test_privkey,
        provider_id="test",
        endpoint_urls=["https://test.com"],
        mint_urls=["https://mint2.com"],
    )
    
    assert not events_semantically_equal(event1, event2)


@pytest.mark.asyncio
async def test_events_semantically_equal_url_order_independent() -> None:
    """Test that URL order doesn't matter for semantic equality."""
    test_privkey = "67dab8473d4c2be1f598a92e8d3c13e6f4f8d0e2b5c6a7b8c9d0e1f2a3b4c5d6"
    
    event1 = create_nip91_event(
        private_key_hex=test_privkey,
        provider_id="test",
        endpoint_urls=["https://test1.com", "https://test2.com"],
    )
    
    event2 = create_nip91_event(
        private_key_hex=test_privkey,
        provider_id="test",
        endpoint_urls=["https://test2.com", "https://test1.com"],
    )
    
    assert events_semantically_equal(event1, event2)


@pytest.mark.asyncio
async def test_discover_onion_url_common_paths() -> None:
    """Test discovering onion URL from common Tor paths."""
    with tempfile.TemporaryDirectory() as tmpdir:
        hostname_path = Path(tmpdir) / "hs" / "router" / "hostname"
        hostname_path.parent.mkdir(parents=True, exist_ok=True)
        hostname_path.write_text("test1234567890abcdef.onion\n")
        
        result = discover_onion_url_from_tor(tmpdir)
        
        assert result == "http://test1234567890abcdef.onion"


@pytest.mark.asyncio
async def test_discover_onion_url_not_found() -> None:
    """Test that None is returned when no onion URL is found."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = discover_onion_url_from_tor(tmpdir)
        assert result is None


@pytest.mark.asyncio
async def test_discover_onion_url_recursive_search() -> None:
    """Test discovering onion URL via recursive directory search."""
    with tempfile.TemporaryDirectory() as tmpdir:
        nested_path = Path(tmpdir) / "some" / "nested" / "directory" / "hostname"
        nested_path.parent.mkdir(parents=True, exist_ok=True)
        nested_path.write_text("nested9876543210fedcba.onion\n")
        
        result = discover_onion_url_from_tor(tmpdir)
        
        assert result == "http://nested9876543210fedcba.onion"


@pytest.mark.asyncio
async def test_discover_onion_url_invalid_hostname() -> None:
    """Test that invalid hostname files are skipped."""
    with tempfile.TemporaryDirectory() as tmpdir:
        hostname_path = Path(tmpdir) / "hs" / "router" / "hostname"
        hostname_path.parent.mkdir(parents=True, exist_ok=True)
        hostname_path.write_text("not-an-onion-address\n")
        
        result = discover_onion_url_from_tor(tmpdir)
        
        assert result is None


@pytest.mark.asyncio
async def test_create_nip91_event_multiple_urls() -> None:
    """Test creating event with multiple endpoint URLs."""
    test_privkey = "67dab8473d4c2be1f598a92e8d3c13e6f4f8d0e2b5c6a7b8c9d0e1f2a3b4c5d6"
    endpoint_urls = ["https://api1.test.com/v1", "https://api2.test.com/v1", "http://onion123.onion"]
    
    event = create_nip91_event(
        private_key_hex=test_privkey,
        provider_id="multi-url-test",
        endpoint_urls=endpoint_urls,
    )
    
    tags = event["tags"]
    u_tags = [tag[1] for tag in tags if tag[0] == "u"]
    assert len(u_tags) == 3
    assert all(url in u_tags for url in endpoint_urls)


@pytest.mark.asyncio
async def test_create_nip91_event_empty_mint_urls() -> None:
    """Test that empty strings in mint_urls are filtered out."""
    test_privkey = "67dab8473d4c2be1f598a92e8d3c13e6f4f8d0e2b5c6a7b8c9d0e1f2a3b4c5d6"
    
    event = create_nip91_event(
        private_key_hex=test_privkey,
        provider_id="test",
        endpoint_urls=["https://test.com"],
        mint_urls=["https://mint1.com", "", "https://mint2.com", ""],
    )
    
    tags = event["tags"]
    mint_tags = [tag[1] for tag in tags if tag[0] == "mint"]
    assert len(mint_tags) == 2
    assert "" not in mint_tags


@pytest.mark.asyncio
async def test_events_semantically_equal_empty_content() -> None:
    """Test semantic equality with empty metadata/content."""
    test_privkey = "67dab8473d4c2be1f598a92e8d3c13e6f4f8d0e2b5c6a7b8c9d0e1f2a3b4c5d6"
    
    event1 = create_nip91_event(
        private_key_hex=test_privkey,
        provider_id="test",
        endpoint_urls=["https://test.com"],
        metadata=None,
    )
    
    event2 = create_nip91_event(
        private_key_hex=test_privkey,
        provider_id="test",
        endpoint_urls=["https://test.com"],
        metadata=None,
    )
    
    assert events_semantically_equal(event1, event2)


@pytest.mark.asyncio
async def test_events_semantically_equal_different_kind() -> None:
    """Test that events with different kinds are not equal."""
    event1: dict[str, Any] = {
        "kind": 38421,
        "tags": [["d", "test"]],
        "content": "",
    }
    
    event2: dict[str, Any] = {
        "kind": 1,
        "tags": [["d", "test"]],
        "content": "",
    }
    
    assert not events_semantically_equal(event1, event2)
