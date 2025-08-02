#!/usr/bin/env python3
"""
Simple Python function to publish one provider listing to a nostr relay
according to the RIP-02 specification.

Based on: https://github.com/Routstr/protocol/blob/main/RIP-02.md
Event Kind: 31338 (Routstr Provider Announcements)
"""

import asyncio
import hashlib
import json
import time
from typing import Any

import secp256k1
import websockets


def create_provider_announcement_event(
    private_key_hex: str,
    provider_name: str,
    endpoint_url: str,
    d_tag: str,
    description: str | None = None,
    contact: str | None = None,
    pricing_url: str | None = None,
    supported_models: list[str] | None = None,
) -> dict[str, Any]:
    """
    Create a RIP-02 compliant provider announcement event.

    Args:
        private_key_hex: 32-byte hex private key for signing
        provider_name: Human readable name for the provider
        endpoint_url: Base URL for the provider's API endpoint
        d_tag: Unique identifier for this provider (required for addressable events)
        description: Optional description of the provider
        contact: Optional contact information
        pricing_url: Optional URL to pricing information
        supported_models: Optional list of supported model names

    Returns:
        Complete signed nostr event ready for publishing
    """
    # Convert hex private key to secp256k1 PrivateKey object
    private_key = secp256k1.PrivateKey(bytes.fromhex(private_key_hex))
    public_key = private_key.pubkey.serialize(compressed=True)[
        1:
    ]  # Remove 0x02/0x03 prefix

    # Build required tags according to RIP-02
    tags = [
        ["d", d_tag],  # Required for addressable events (kind 30000-39999)
        ["endpoint", endpoint_url],
        ["name", provider_name],
    ]

    # Add optional tags if provided
    if description:
        tags.append(["description", description])
    if contact:
        tags.append(["contact", contact])
    if pricing_url:
        tags.append(["pricing", pricing_url])
    if supported_models:
        for model in supported_models:
            tags.append(["model", model])

    # Create the event structure
    created_at = int(time.time())
    event_data = [
        0,  # Reserved field
        public_key.hex(),  # Public key as hex
        created_at,  # Unix timestamp
        31338,  # Kind for RIP-02 Provider Announcements
        tags,  # Tags array
        "",  # Content (empty for provider announcements)
    ]

    # Serialize event data for hashing
    event_json = json.dumps(event_data, separators=(",", ":"), ensure_ascii=False)

    # Calculate event ID (SHA256 hash)
    event_id = hashlib.sha256(event_json.encode("utf-8")).hexdigest()

    # Sign the event ID
    signature = private_key.ecdsa_sign(bytes.fromhex(event_id), raw=True)
    signature_der = private_key.ecdsa_serialize(signature)

    # Create the final event
    event = {
        "id": event_id,
        "pubkey": public_key.hex(),
        "created_at": created_at,
        "kind": 31338,
        "tags": tags,
        "content": "",
        "sig": signature_der.hex(),
    }

    return event


async def publish_provider_to_relay(
    relay_url: str, event: dict[str, Any], timeout: int = 30
) -> bool:
    """
    Publish a provider announcement event to a nostr relay.

    Args:
        relay_url: WebSocket URL of the nostr relay (e.g., "wss://relay.damus.io")
        event: Complete signed nostr event to publish
        timeout: Connection timeout in seconds

    Returns:
        True if successfully published, False otherwise
    """
    try:
        async with websockets.connect(relay_url, timeout=timeout) as websocket:
            # Send EVENT message
            event_message = json.dumps(["EVENT", event])
            await websocket.send(event_message)
            print(f"Published event {event['id']} to {relay_url}")

            # Wait for OK response
            try:
                response = await asyncio.wait_for(websocket.recv(), timeout=5)
                data = json.loads(response)

                if data[0] == "OK" and data[1] == event["id"]:
                    if data[2]:  # True means accepted
                        print(
                            f"✅ Event accepted by relay: {data[3] if len(data) > 3 else ''}"
                        )
                        return True
                    else:
                        print(
                            f"❌ Event rejected by relay: {data[3] if len(data) > 3 else ''}"
                        )
                        return False
                elif data[0] == "NOTICE":
                    print(f"📢 Relay notice: {data[1]}")
                    return False
                else:
                    print(f"🤔 Unexpected response: {data}")
                    return False

            except asyncio.TimeoutError:
                print("⏰ No response from relay within timeout")
                return False

    except Exception as e:
        print(f"💥 Failed to publish to {relay_url}: {e}")
        return False


async def publish_provider_listing(
    private_key_hex: str,
    provider_name: str,
    endpoint_url: str,
    d_tag: str,
    relay_urls: list[str] | None = None,
    description: str | None = None,
    contact: str | None = None,
    pricing_url: str | None = None,
    supported_models: list[str] | None = None,
) -> dict[str, bool]:
    """
    Complete function to create and publish a provider listing to nostr relays.

    Args:
        private_key_hex: 32-byte hex private key for signing
        provider_name: Human readable name for the provider
        endpoint_url: Base URL for the provider's API endpoint
        d_tag: Unique identifier for this provider
        relay_urls: List of relay URLs to publish to (uses defaults if None)
        description: Optional description of the provider
        contact: Optional contact information
        pricing_url: Optional URL to pricing information
        supported_models: Optional list of supported model names

    Returns:
        Dictionary mapping relay URLs to success status
    """
    # Use default relays if none provided
    if relay_urls is None:
        relay_urls = [
            "wss://relay.nostr.band",
            "wss://relay.damus.io",
            "wss://relay.routstr.com",
        ]

    # Create the provider announcement event
    event = create_provider_announcement_event(
        private_key_hex=private_key_hex,
        provider_name=provider_name,
        endpoint_url=endpoint_url,
        d_tag=d_tag,
        description=description,
        contact=contact,
        pricing_url=pricing_url,
        supported_models=supported_models,
    )

    print(f"📝 Created provider announcement event: {event['id']}")
    print(f"🔑 Public key: {event['pubkey']}")
    print(f"🏷️  Provider: {provider_name}")
    print(f"🌐 Endpoint: {endpoint_url}")
    print()

    # Publish to all specified relays
    results = {}
    tasks = []

    for relay_url in relay_urls:
        task = publish_provider_to_relay(relay_url, event)
        tasks.append((relay_url, task))

    # Execute all publishing tasks concurrently
    for relay_url, task in tasks:
        try:
            success = await task
            results[relay_url] = success
        except Exception as e:
            print(f"💥 Failed to publish to {relay_url}: {e}")
            results[relay_url] = False

    return results


# Example usage
async def main() -> None:
    """Example of how to use the provider publishing function."""

    # Example private key (DO NOT use this in production!)
    private_key = "3185a47e3802f956ca207b46c8d6b8b5c5dbad53a5ca29816050e9b66badc33c"

    # Example provider information
    provider_name = "My AI Provider"
    endpoint_url = "https://api.myaiprovider.com"
    d_tag = "my-ai-provider-v1"  # Unique identifier
    description = "High-quality AI models with competitive pricing"
    contact = "admin@myaiprovider.com"
    pricing_url = "https://myaiprovider.com/pricing"
    supported_models = ["gpt-4o", "claude-3-sonnet", "llama-3.1-70b"]

    # Publish to relays
    results = await publish_provider_listing(
        private_key_hex=private_key,
        provider_name=provider_name,
        endpoint_url=endpoint_url,
        d_tag=d_tag,
        description=description,
        contact=contact,
        pricing_url=pricing_url,
        supported_models=supported_models,
    )

    # Print results
    print("\n📊 Publishing Results:")
    for relay_url, success in results.items():
        status = "✅ Success" if success else "❌ Failed"
        print(f"  {relay_url}: {status}")


if __name__ == "__main__":
    asyncio.run(main())
