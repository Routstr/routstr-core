import asyncio
import json
import os
import random
import string
from typing import Any

import httpx
import websockets
from fastapi import APIRouter

providers_router = APIRouter(prefix="/v1/providers")


def generate_subscription_id() -> str:
    """Generate a random subscription ID."""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=10))


async def query_nostr_relay_for_providers(
    relay_url: str,
    pubkey: str | None = None,
    limit: int = 1000,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """
    Query a Nostr relay for provider announcements.
    Searches for NIP-91 (kind:38421) events.
    """
    events = []

    # Build filter for NIP-91 events
    filter_obj: dict[str, Any] = {
        "kinds": [38421],  # NIP-91 Provider Announcements
        "limit": limit,
    }

    # If specific pubkey provided, filter by author
    if pubkey:
        filter_obj["authors"] = [pubkey]

    sub_id = generate_subscription_id()
    req_message = json.dumps(["REQ", sub_id, filter_obj])

    try:
        async with websockets.connect(relay_url, timeout=timeout) as websocket:
            print("Connected to relay, searching for provider announcement events")
            await websocket.send(req_message)

            while True:
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=5)
                    data = json.loads(message)

                    if data[0] == "EVENT" and data[1] == sub_id:
                        event = data[2]
                        print(f"Found provider announcement: {event['id']}")
                        events.append(event)
                    elif data[0] == "EOSE" and data[1] == sub_id:
                        print("Received EOSE message")
                        break
                    elif data[0] == "NOTICE":
                        print(f"Relay notice: {data[1]}")

                except asyncio.TimeoutError:
                    print("Timeout waiting for message")
                    break
                except json.JSONDecodeError:
                    print("Failed to decode message as JSON")
                    continue

            await websocket.send(json.dumps(["CLOSE", sub_id]))

    except Exception as e:
        print(f"Query failed: {e}")

    print(f"Query complete. Found {len(events)} provider announcements")
    return events


def parse_provider_announcement(event: dict[str, Any]) -> dict[str, Any] | None:
    """
    Parse provider announcement events.
    Handles NIP-91 (kind:38421) format.
    Returns structured provider data or None if invalid.
    """
    try:
        tags = event.get("tags", [])
        kind = event.get("kind")
        
        # Common fields
        d_tag = None
        endpoint_urls = []
        provider_name = None
        description = None
        contact = None
        pricing_url = None
        supported_models = []
        mint_url = None
        version = None
        
        # Parse NIP-91 format
        if kind == 38421:  # NIP-91 format
            for tag in tags:
                if len(tag) >= 2:
                    if tag[0] == "d":
                        d_tag = tag[1]
                    elif tag[0] == "u":
                        endpoint_urls.append(tag[1])
                    elif tag[0] == "models" and len(tag) > 1:
                        # NIP-91 uses single models tag with multiple values
                        supported_models = tag[1:]
                    elif tag[0] == "mint":
                        mint_url = tag[1]
                    elif tag[0] == "version":
                        version = tag[1]
            
            # Parse metadata from content for NIP-91
            content = event.get("content", "")
            if content:
                try:
                    metadata = json.loads(content)
                    provider_name = metadata.get("name", "Unknown Provider")
                    description = metadata.get("about")
                    contact = metadata.get("contact")
                except (json.JSONDecodeError, TypeError):
                    provider_name = "Unknown Provider"
            else:
                provider_name = "Unknown Provider"
            
            # Use first URL as primary endpoint
            endpoint_url = endpoint_urls[0] if endpoint_urls else None
            
            # Validate NIP-91 required fields
            if not endpoint_url or not d_tag:
                print(f"Invalid NIP-91 announcement - missing required fields: {event['id']}")
                return None
        else:
            print(f"Unknown event kind: {kind}")
            return None

        return {
            "id": event["id"],
            "pubkey": event["pubkey"],
            "created_at": event["created_at"],
            "kind": kind,
            "d_tag": d_tag,
            "endpoint_url": endpoint_url,
            "endpoint_urls": endpoint_urls,  # All URLs for NIP-91
            "name": provider_name,
            "description": description,
            "contact": contact,
            "pricing_url": pricing_url,
            "mint_url": mint_url,
            "version": version,
            "supported_models": supported_models,
            "content": event.get("content", ""),
        }

    except Exception as e:
        print(f"Error parsing provider announcement {event.get('id', 'unknown')}: {e}")
        return None


async def get_cache() -> list[dict[str, Any]]:
    return []  # TODO: Implement cache


async def fetch_provider_health(endpoint_url: str) -> dict[str, Any]:
    """Check if a provider endpoint is healthy by making a GET request."""
    try:
        # Determine if we need Tor proxy based on .onion domain
        is_onion = ".onion" in endpoint_url

        # Set up client arguments conditionally
        proxies = None
        if is_onion:
            # Get Tor proxy URL from environment variable
            tor_proxy = os.getenv("TOR_PROXY_URL", "socks5://127.0.0.1:9050")
            proxies = {"http://": tor_proxy, "https://": tor_proxy}  # type: ignore[assignment]

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            proxies=proxies,  # type: ignore[arg-type]
        ) as client:
            # Try to fetch models endpoint first (common for AI providers)
            models_url = f"{endpoint_url.rstrip('/')}/v1/models"
            try:
                response = await client.get(models_url)
                if response.status_code == 200:
                    return {
                        "status_code": response.status_code,
                        "endpoint": "models",
                        "json": response.json(),
                    }
            except Exception:
                pass

            # Fallback to root endpoint
            response = await client.get(endpoint_url)
            return {
                "status_code": response.status_code,
                "endpoint": "root",
                "json": response.json()
                if response.headers.get("content-type", "").startswith(
                    "application/json"
                )
                else {"message": "OK"},
            }

    except Exception as e:
        return {
            "status_code": 500,
            "endpoint": "error",
            "json": {"error": f"Failed to fetch provider: {str(e)}"},
        }


@providers_router.get("/")
async def get_providers(
    include_json: bool = False, pubkey: str | None = None
) -> dict[str, list[dict[str, Any]]]:
    """
    Discover Routstr providers using NIP-91 specification.
    Searches for provider announcement events on Nostr relays:
    - kind:38421 (NIP-91)

    References:
    - NIP-91: https://github.com/nostr-protocol/nips/pull/1987
    """
    # Default relays for provider discovery
    discovery_relays = [
        "wss://relay.nostr.band",
        "wss://relay.damus.io",
        "wss://relay.routstr.com",
    ]

    all_events = []
    event_ids = set()  # To avoid duplicates

    # Query multiple relays for provider announcements
    for relay_url in discovery_relays:
        print(f"\nQuerying relay for providers: {relay_url}")
        try:
            events = await query_nostr_relay_for_providers(
                relay_url=relay_url,
                pubkey=pubkey,
                limit=100,
            )

            # Add unique events
            for event in events:
                if event["id"] not in event_ids:
                    event_ids.add(event["id"])
                    all_events.append(event)

            print(f"Got {len(events)} provider announcements from {relay_url}")

        except Exception as e:
            print(f"Failed to query {relay_url}: {e}")
            continue

    print(f"Found {len(all_events)} total unique provider announcements")

    # Parse provider announcements according to NIP-91
    providers = []
    for event in all_events:
        parsed_provider = parse_provider_announcement(event)
        if parsed_provider:
            providers.append(parsed_provider)

    print(f"Parsed {len(providers)} valid provider announcements")

    # Check provider health if requested
    healthy_providers: list[dict[str, Any]] = []
    for provider in providers:
        endpoint_url = provider["endpoint_url"]

        if include_json:
            health_check = await fetch_provider_health(endpoint_url)
            provider_data = {"provider": provider, "health": health_check}
            healthy_providers.append(provider_data)
        else:
            # Just return the provider info without health check
            healthy_providers.append(provider)

    return {"providers": healthy_providers}
