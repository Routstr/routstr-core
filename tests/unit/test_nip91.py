"""Unit tests for NIP-91 provider announcement functionality."""

import pytest

from routstr.nip91 import (
    create_nip91_event,
    events_semantically_equal,
    nsec_to_keypair,
)


def test_nsec_to_keypair_valid_nsec() -> None:
    """Test converting valid nsec to keypair."""
    nsec = "nsec1testkey1234567890abcdefghijklmnopqrstuvwxyz"
    
    result = nsec_to_keypair(nsec)
    
    assert result is not None
    privkey, pubkey = result
    assert isinstance(privkey, str)
    assert isinstance(pubkey, str)
    assert len(privkey) == 64
    assert len(pubkey) == 64


def test_nsec_to_keypair_hex_format() -> None:
    """Test converting hex format private key."""
    hex_key = "a" * 64
    
    result = nsec_to_keypair(hex_key)
    
    assert result is not None
    privkey, pubkey = result
    assert isinstance(privkey, str)
    assert isinstance(pubkey, str)


def test_nsec_to_keypair_invalid_format() -> None:
    """Test converting invalid format nsec."""
    invalid_nsec = "invalid_key"
    
    result = nsec_to_keypair(invalid_nsec)
    
    assert result is None


def test_nsec_to_keypair_empty_string() -> None:
    """Test converting empty string."""
    result = nsec_to_keypair("")
    
    assert result is None


def test_create_nip91_event_structure() -> None:
    """Test NIP-91 event structure."""
    private_key_hex = "a" * 64
    provider_id = "test-provider"
    endpoint_urls = ["https://example.com"]
    
    event = create_nip91_event(
        private_key_hex=private_key_hex,
        provider_id=provider_id,
        endpoint_urls=endpoint_urls,
    )
    
    assert isinstance(event, dict)
    assert event["kind"] == 38421
    assert "id" in event
    assert "pubkey" in event
    assert "created_at" in event
    assert "tags" in event
    assert "content" in event
    assert "sig" in event
    
    tags = event["tags"]
    d_tag = [tag for tag in tags if tag[0] == "d"]
    assert len(d_tag) == 1
    assert d_tag[0][1] == provider_id
    
    u_tags = [tag for tag in tags if tag[0] == "u"]
    assert len(u_tags) == 1
    assert u_tags[0][1] == endpoint_urls[0]


def test_create_nip91_event_signature() -> None:
    """Test that NIP-91 event is properly signed."""
    private_key_hex = "a" * 64
    provider_id = "test-provider"
    endpoint_urls = ["https://example.com"]
    
    event = create_nip91_event(
        private_key_hex=private_key_hex,
        provider_id=provider_id,
        endpoint_urls=endpoint_urls,
    )
    
    assert "sig" in event
    assert len(event["sig"]) == 128


def test_create_nip91_event_with_mint_urls() -> None:
    """Test creating NIP-91 event with mint URLs."""
    private_key_hex = "a" * 64
    provider_id = "test-provider"
    endpoint_urls = ["https://example.com"]
    mint_urls = ["https://mint.example.com"]
    
    event = create_nip91_event(
        private_key_hex=private_key_hex,
        provider_id=provider_id,
        endpoint_urls=endpoint_urls,
        mint_urls=mint_urls,
    )
    
    tags = event["tags"]
    mint_tags = [tag for tag in tags if tag[0] == "mint"]
    assert len(mint_tags) == 1
    assert mint_tags[0][1] == mint_urls[0]


def test_create_nip91_event_with_metadata() -> None:
    """Test creating NIP-91 event with metadata."""
    private_key_hex = "a" * 64
    provider_id = "test-provider"
    endpoint_urls = ["https://example.com"]
    metadata = {"name": "Test Provider", "about": "Test description"}
    
    event = create_nip91_event(
        private_key_hex=private_key_hex,
        provider_id=provider_id,
        endpoint_urls=endpoint_urls,
        metadata=metadata,
    )
    
    assert event["content"] != ""
    import json
    
    content = json.loads(event["content"])
    assert content["name"] == metadata["name"]
    assert content["about"] == metadata["about"]


def test_events_semantically_equal_identical() -> None:
    """Test that identical events are semantically equal."""
    private_key_hex = "a" * 64
    provider_id = "test-provider"
    endpoint_urls = ["https://example.com"]
    
    event1 = create_nip91_event(
        private_key_hex=private_key_hex,
        provider_id=provider_id,
        endpoint_urls=endpoint_urls,
    )
    
    event2 = create_nip91_event(
        private_key_hex=private_key_hex,
        provider_id=provider_id,
        endpoint_urls=endpoint_urls,
    )
    
    assert events_semantically_equal(event1, event2)


def test_events_semantically_equal_different_timestamps() -> None:
    """Test that events with different timestamps are still semantically equal."""
    private_key_hex = "a" * 64
    provider_id = "test-provider"
    endpoint_urls = ["https://example.com"]
    
    event1 = create_nip91_event(
        private_key_hex=private_key_hex,
        provider_id=provider_id,
        endpoint_urls=endpoint_urls,
    )
    
    import time
    time.sleep(1)
    
    event2 = create_nip91_event(
        private_key_hex=private_key_hex,
        provider_id=provider_id,
        endpoint_urls=endpoint_urls,
    )
    
    assert events_semantically_equal(event1, event2)


def test_events_semantically_equal_different_content() -> None:
    """Test that events with different content are not semantically equal."""
    private_key_hex = "a" * 64
    provider_id = "test-provider"
    endpoint_urls = ["https://example.com"]
    
    event1 = create_nip91_event(
        private_key_hex=private_key_hex,
        provider_id=provider_id,
        endpoint_urls=endpoint_urls,
        metadata={"name": "Provider 1"},
    )
    
    event2 = create_nip91_event(
        private_key_hex=private_key_hex,
        provider_id=provider_id,
        endpoint_urls=endpoint_urls,
        metadata={"name": "Provider 2"},
    )
    
    assert not events_semantically_equal(event1, event2)


def test_events_semantically_equal_different_urls() -> None:
    """Test that events with different URLs are not semantically equal."""
    private_key_hex = "a" * 64
    provider_id = "test-provider"
    
    event1 = create_nip91_event(
        private_key_hex=private_key_hex,
        provider_id=provider_id,
        endpoint_urls=["https://example.com"],
    )
    
    event2 = create_nip91_event(
        private_key_hex=private_key_hex,
        provider_id=provider_id,
        endpoint_urls=["https://different.com"],
    )
    
    assert not events_semantically_equal(event1, event2)
