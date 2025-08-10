import asyncio
import json
import os
import random
import string
from typing import Any

import httpx
import websockets
from fastapi import APIRouter

from .core.logging import get_logger
from .core.settings import SettingsManager

logger = get_logger(__name__)

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
    Query a Nostr relay for provider announcements using RIP-02 spec.
    Searches for kind 31338 events (Routstr Provider Announcements).
    """
    events = []

    # Build filter according to RIP-02 spec
    filter_obj: dict[str, Any] = {
        "kinds": [31338],  # RIP-02 Provider Announcement events
        "limit": limit,
    }

    # If specific pubkey provided, filter by author
    if pubkey:
        filter_obj["authors"] = [pubkey]

    sub_id = generate_subscription_id()
    req_message = json.dumps(["REQ", sub_id, filter_obj])

    try:
        async with websockets.connect(relay_url, timeout=timeout) as websocket:
            print("Connected to relay, searching for kind 31338 events")
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
    Parse a kind 31338 provider announcement event according to RIP-02 spec.
    Returns structured provider data or None if invalid.
    """
    try:
        # Extract required tags according to RIP-02
        tags = event.get("tags", [])

        # Find required tags
        endpoint_url = None
        provider_name = None
        d_tag = None

        for tag in tags:
            if len(tag) >= 2:
                if tag[0] == "endpoint":
                    endpoint_url = tag[1]
                elif tag[0] == "name":
                    provider_name = tag[1]
                elif tag[0] == "d":
                    d_tag = tag[1]

        # Validate required fields
        if not endpoint_url or not provider_name or not d_tag:
            print(
                f"Invalid provider announcement - missing required tags: {event['id']}"
            )
            return None

        # Extract optional tags
        description = None
        contact = None
        pricing_url = None
        supported_models = []

        for tag in tags:
            if len(tag) >= 2:
                if tag[0] == "description":
                    description = tag[1]
                elif tag[0] == "contact":
                    contact = tag[1]
                elif tag[0] == "pricing":
                    pricing_url = tag[1]
                elif tag[0] == "model":
                    supported_models.append(tag[1])

        return {
            "id": event["id"],
            "pubkey": event["pubkey"],
            "created_at": event["created_at"],
            "d_tag": d_tag,
            "endpoint_url": endpoint_url,
            "name": provider_name,
            "description": description,
            "contact": contact,
            "pricing_url": pricing_url,
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
            # Get Tor proxy URL from settings
            tor_proxy = await SettingsManager.get("TOR_PROXY_URL", "socks5://127.0.0.1:9050")
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
    Discover Routstr providers using RIP-02 specification.
    Searches for kind 31338 provider announcement events on Nostr relays.

    Reference: https://github.com/Routstr/protocol/blob/main/RIP-02.md
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

    # Parse provider announcements according to RIP-02
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
