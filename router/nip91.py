#!/usr/bin/env python3
"""
NIP-91: Routstr Provider Discoverability Implementation
Automatically announces this Routstr proxy instance to Nostr relays.
"""

import asyncio
import hashlib
import json
import os
import time
from typing import Any

import secp256k1
import websockets

from .core import get_logger
from .payment.models import MODELS

logger = get_logger(__name__)


def nsec_to_keypair(nsec: str) -> tuple[str, str] | None:
    """
    Convert a Nostr private key (nsec) to a keypair (privkey_hex, pubkey_hex).
    
    Args:
        nsec: Nostr private key in nsec format or hex format
    
    Returns:
        Tuple of (private_key_hex, public_key_hex) or None if invalid
    """
    try:
        # Handle nsec format
        if nsec.startswith("nsec"):
            # Simple bech32 decode - for production use a proper library
            # For now, we'll assume hex format is passed
            logger.warning("nsec format not yet implemented, please use hex format")
            return None
        
        # Assume hex format
        if len(nsec) != 64:
            logger.error(f"Invalid private key length: {len(nsec)}")
            return None
            
        private_key = secp256k1.PrivateKey(bytes.fromhex(nsec))
        public_key = private_key.pubkey.serialize(compressed=True)[1:]  # Remove 0x02/0x03 prefix
        
        return (nsec, public_key.hex())
    except Exception as e:
        logger.error(f"Failed to convert nsec to keypair: {e}")
        return None


def create_nip91_event(
    private_key_hex: str,
    provider_id: str,
    endpoint_urls: list[str],
    supported_models: list[str],
    mint_url: str | None = None,
    version: str = "0.1.0",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Create a NIP-91 compliant provider announcement event (kind:38421).
    
    Args:
        private_key_hex: 32-byte hex private key for signing
        provider_id: Unique identifier for this provider (d tag)
        endpoint_urls: List of URLs to connect to the provider
        supported_models: List of supported AI model IDs
        mint_url: Optional ecash mint URL for payments
        version: Provider software version
        metadata: Optional metadata dictionary (name, picture, about, etc.)
    
    Returns:
        Complete signed nostr event ready for publishing
    """
    # Convert hex private key to secp256k1 PrivateKey object
    private_key = secp256k1.PrivateKey(bytes.fromhex(private_key_hex))
    public_key = private_key.pubkey.serialize(compressed=True)[1:]  # Remove 0x02/0x03 prefix
    
    # Build tags according to NIP-91
    tags = [
        ["d", provider_id],  # Unique identifier
    ]
    
    # Add URLs
    for url in endpoint_urls:
        tags.append(["u", url])
    
    # Add models as a single tag with multiple values
    if supported_models:
        tags.append(["models"] + supported_models)
    
    # Add optional tags
    if mint_url:
        tags.append(["mint", mint_url])
    tags.append(["version", version])
    
    # Add model capabilities if detailed info available
    for model in MODELS:
        if model.id in supported_models:
            capabilities = []
            
            # Add max_tokens from context_length
            if model.context_length:
                capabilities.append(f"max_tokens:{model.context_length}")
            
            # Check if model supports vision (simplified check)
            if any(modal in ["image"] for modal in model.architecture.input_modalities):
                capabilities.append("vision:true")
            else:
                capabilities.append("vision:false")
            
            # Check if model supports tools (simplified - most modern models do)
            if "gpt" in model.id or "claude" in model.id or "llama" in model.id:
                capabilities.append("tools:true")
            else:
                capabilities.append("tools:false")
            
            if capabilities:
                tags.append(["model-cap", model.id, ",".join(capabilities)])
    
    # Content is optional metadata as JSON string
    content = ""
    if metadata:
        content = json.dumps(metadata, separators=(",", ":"))
    
    # Create the event structure
    created_at = int(time.time())
    event_data = [
        0,  # Reserved field
        public_key.hex(),  # Public key as hex
        created_at,  # Unix timestamp
        38421,  # Kind for NIP-91 Provider Announcements
        tags,  # Tags array
        content,  # Content (metadata)
    ]
    
    # Serialize event data for hashing
    event_json = json.dumps(event_data, separators=(",", ":"), ensure_ascii=False)
    
    # Calculate event ID (SHA256 hash)
    event_id = hashlib.sha256(event_json.encode("utf-8")).hexdigest()
    
    # Sign the event ID
    signature = private_key.ecdsa_sign(bytes.fromhex(event_id), raw=True)
    signature_ser = private_key.ecdsa_serialize(signature)
    
    # Create the final event
    event = {
        "id": event_id,
        "pubkey": public_key.hex(),
        "created_at": created_at,
        "kind": 38421,
        "tags": tags,
        "content": content,
        "sig": signature_ser.hex(),
    }
    
    return event


async def query_nip91_events(
    relay_url: str,
    pubkey: str,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """
    Query a Nostr relay for NIP-91 provider announcements (kind:38421).
    
    Args:
        relay_url: WebSocket URL of the nostr relay
        pubkey: Public key to filter by
        timeout: Connection timeout in seconds
    
    Returns:
        List of NIP-91 events from the given pubkey
    """
    events = []
    
    # Build filter for NIP-91 events from specific pubkey
    filter_obj = {
        "kinds": [38421],
        "authors": [pubkey],
        "limit": 10,
    }
    
    sub_id = f"nip91_{int(time.time())}"
    req_message = json.dumps(["REQ", sub_id, filter_obj])
    
    try:
        async with websockets.connect(relay_url, timeout=timeout) as websocket:
            logger.info(f"Querying {relay_url} for existing NIP-91 events")
            await websocket.send(req_message)
            
            while True:
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=5)
                    data = json.loads(message)
                    
                    if data[0] == "EVENT" and data[1] == sub_id:
                        event = data[2]
                        logger.info(f"Found existing NIP-91 event: {event['id']}")
                        events.append(event)
                    elif data[0] == "EOSE" and data[1] == sub_id:
                        logger.debug("Received EOSE message")
                        break
                    elif data[0] == "NOTICE":
                        logger.warning(f"Relay notice: {data[1]}")
                
                except asyncio.TimeoutError:
                    logger.debug("Timeout waiting for relay response")
                    break
                except json.JSONDecodeError:
                    logger.error("Failed to decode relay message as JSON")
                    continue
            
            await websocket.send(json.dumps(["CLOSE", sub_id]))
    
    except Exception as e:
        logger.error(f"Failed to query relay {relay_url}: {e}")
    
    return events


async def publish_to_relay(
    relay_url: str,
    event: dict[str, Any],
    timeout: int = 30,
) -> bool:
    """
    Publish a NIP-91 event to a nostr relay.
    
    Args:
        relay_url: WebSocket URL of the nostr relay
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
            logger.info(f"Sent NIP-91 event {event['id']} to {relay_url}")
            
            # Wait for OK response
            try:
                response = await asyncio.wait_for(websocket.recv(), timeout=5)
                data = json.loads(response)
                
                if data[0] == "OK" and data[1] == event["id"]:
                    if data[2]:  # True means accepted
                        logger.info(f"Event accepted by {relay_url}: {data[3] if len(data) > 3 else ''}")
                        return True
                    else:
                        logger.warning(f"Event rejected by {relay_url}: {data[3] if len(data) > 3 else ''}")
                        return False
                elif data[0] == "NOTICE":
                    logger.warning(f"Relay notice from {relay_url}: {data[1]}")
                    return False
                else:
                    logger.warning(f"Unexpected response from {relay_url}: {data}")
                    return False
            
            except asyncio.TimeoutError:
                logger.warning(f"No response from {relay_url} within timeout")
                return False
    
    except Exception as e:
        logger.error(f"Failed to publish to {relay_url}: {e}")
        return False


async def announce_provider() -> None:
    """
    Background task to announce this Routstr provider to Nostr relays.
    Checks for existing announcements and creates new ones if needed.
    """
    # Check for NSEC in environment
    nsec = os.getenv("NOSTR_NSEC")
    if not nsec:
        logger.info("NOSTR_NSEC not found in environment, skipping NIP-91 announcement")
        return
    
    # Convert NSEC to keypair
    keypair = nsec_to_keypair(nsec)
    if not keypair:
        logger.error("Failed to parse NOSTR_NSEC, skipping NIP-91 announcement")
        return
    
    private_key_hex, public_key_hex = keypair
    logger.info(f"Using Nostr pubkey: {public_key_hex}")
    
    # Get configuration from environment
    provider_id = os.getenv("ROUTSTR_PROVIDER_ID", os.getenv("HOSTNAME", "routstr-proxy"))
    base_url = os.getenv("ROUTSTR_BASE_URL", "http://localhost:8000")
    onion_url = os.getenv("ROUTSTR_ONION_URL")
    mint_url = os.getenv("ROUTSTR_MINT_URL")
    provider_name = os.getenv("ROUTSTR_PROVIDER_NAME", "Routstr Proxy")
    provider_about = os.getenv("ROUTSTR_PROVIDER_ABOUT", "Privacy-preserving AI proxy via Nostr")
    
    # Build endpoint URLs
    endpoint_urls = [base_url]
    if onion_url:
        endpoint_urls.append(onion_url)
    
    # Get supported models
    supported_models = [model.id for model in MODELS]
    if not supported_models:
        logger.warning("No models loaded, will announce with empty model list")
        supported_models = []
    
    # Build metadata
    metadata = {
        "name": provider_name,
        "about": provider_about,
    }
    
    # Get relay URLs from environment or use defaults
    relay_urls_env = os.getenv("NOSTR_RELAYS")
    if relay_urls_env:
        relay_urls = [url.strip() for url in relay_urls_env.split(",")]
    else:
        relay_urls = [
            "wss://relay.nostr.band",
            "wss://relay.damus.io", 
            "wss://nos.lol",
        ]
    
    # Check for existing announcements
    existing_events = []
    for relay_url in relay_urls:
        events = await query_nip91_events(relay_url, public_key_hex)
        existing_events.extend(events)
    
    # Check if we need to publish (no events or outdated)
    should_publish = True
    if existing_events:
        # Check if any existing event matches our current configuration
        for event in existing_events:
            tags_dict = {tag[0]: tag[1:] for tag in event.get("tags", [])}
            if tags_dict.get("d", [""])[0] == provider_id:
                # Check if configuration has changed
                existing_urls = [tag[1] for tag in event.get("tags", []) if tag[0] == "u"]
                existing_models: list[str] = next((tag[1:] for tag in event.get("tags", []) if tag[0] == "models"), [])
                
                if set(existing_urls) == set(endpoint_urls) and set(existing_models) == set(supported_models):
                    logger.info("Existing NIP-91 announcement is up to date")
                    should_publish = False
                    break
    
    if should_publish:
        # Create new NIP-91 event
        event = create_nip91_event(
            private_key_hex=private_key_hex,
            provider_id=provider_id,
            endpoint_urls=endpoint_urls,
            supported_models=supported_models,
            mint_url=mint_url,
            version=os.getenv("ROUTSTR_VERSION", "0.1.0"),
            metadata=metadata,
        )
        
        logger.info(f"Created NIP-91 announcement event: {event['id']}")
        
        # Publish to all relays
        success_count = 0
        for relay_url in relay_urls:
            if await publish_to_relay(relay_url, event):
                success_count += 1
        
        logger.info(f"Published NIP-91 announcement to {success_count}/{len(relay_urls)} relays")
    
    # Re-announce periodically (every 24 hours)
    announcement_interval = int(os.getenv("NIP91_ANNOUNCEMENT_INTERVAL", str(24 * 60 * 60)))
    
    while True:
        try:
            await asyncio.sleep(announcement_interval)
            
            # Re-create and publish event
            event = create_nip91_event(
                private_key_hex=private_key_hex,
                provider_id=provider_id,
                endpoint_urls=endpoint_urls,
                supported_models=[model.id for model in MODELS],  # Refresh model list
                mint_url=mint_url,
                version=os.getenv("ROUTSTR_VERSION", "0.1.0"),
                metadata=metadata,
            )
            
            logger.info(f"Re-announcing provider (periodic update): {event['id']}")
            
            for relay_url in relay_urls:
                await publish_to_relay(relay_url, event)
                
        except asyncio.CancelledError:
            logger.info("NIP-91 announcement task cancelled")
            break
        except Exception as e:
            logger.error(f"Error in NIP-91 announcement loop: {e}")
            # Continue running despite errors