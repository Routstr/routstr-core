import asyncio
import json
import random
import string
from typing import Any

import httpx
import websockets
from fastapi import APIRouter

from .core.logging import get_logger
from .core.settings import settings

logger = get_logger(__name__)

providers_router = APIRouter(prefix="/v1/providers")

# In-memory providers cache and lock
_PROVIDERS_CACHE: list[dict[str, Any]] = []
_PROVIDERS_CACHE_LOCK = asyncio.Lock()


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
        async with websockets.connect(relay_url, open_timeout=timeout) as websocket:
            logger.debug("Connected to relay, searching for NIP-91 events (kind 38421)")
            await websocket.send(req_message)

            while True:
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=5)
                    data = json.loads(message)

                    if data[0] == "EVENT" and data[1] == sub_id:
                        event = data[2]
                        logger.debug(f"Found provider announcement: {event['id']}")
                        events.append(event)
                    elif data[0] == "EOSE" and data[1] == sub_id:
                        logger.debug("Received EOSE message")
                        break
                    elif data[0] == "NOTICE":
                        try:
                            msg = str(data[1])
                            if len(msg) > 200:
                                msg = msg[:200] + "..."
                            logger.debug(f"Relay notice: {msg}")
                        except Exception:
                            logger.debug("Relay notice received")

                except asyncio.TimeoutError:
                    logger.debug("Timeout waiting for message")
                    break
                except json.JSONDecodeError:
                    logger.warning("Failed to decode message as JSON")
                    continue

            await websocket.send(json.dumps(["CLOSE", sub_id]))

    except Exception as e:
        logger.debug(f"Query failed: {type(e).__name__}")

    logger.info(f"Query complete. Found {len(events)} provider announcements")
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
        endpoint_url = None

        for tag in tags:
            if len(tag) >= 2:
                if tag[0] == "endpoint":
                    endpoint_url = tag[1]
                elif tag[0] == "name":
                    provider_name = tag[1]
                elif tag[0] == "d":
                    d_tag = tag[1]

        # Early validation only applies to legacy/other kinds, not NIP-91
        if kind != 38421 and (not endpoint_url or not provider_name or not d_tag):
            logger.warning(
                f"Invalid provider announcement - missing required tags: {event['id']}"
            )
            return None

        # Extract optional tags
        description = None
        mint_urls = []
        version = None

        # Parse NIP-91 format
        if kind == 38421:  # NIP-91 format
            for tag in tags:
                if len(tag) >= 2:
                    if tag[0] == "d":
                        d_tag = tag[1]
                    elif tag[0] == "u":
                        endpoint_urls.append(tag[1])
                    elif tag[0] == "mint":
                        mint_urls.append(tag[1])
                    elif tag[0] == "version":
                        version = tag[1]

            # Parse metadata from content for NIP-91
            content = event.get("content", "")
            if content:
                try:
                    metadata = json.loads(content)
                    provider_name = metadata.get("name", "Unknown Provider")
                    description = metadata.get("about")
                except (json.JSONDecodeError, TypeError):
                    provider_name = "Unknown Provider"
            else:
                provider_name = "Unknown Provider"

            # Use first URL as primary endpoint
            endpoint_url = endpoint_urls[0] if endpoint_urls else None

            # Validate NIP-91 required fields
            if not endpoint_url or not d_tag:
                logger.warning(
                    f"Invalid NIP-91 announcement - missing required fields: {event['id']}"
                )
                return None
        else:
            logger.warning(
                f"Unknown event kind when parsing provider announcement: {kind}"
            )
            return None

        return {
            "id": d_tag,
            "pubkey": event["pubkey"],
            "created_at": event["created_at"],
            "kind": kind,
            "endpoint_url": endpoint_url,
            "endpoint_urls": endpoint_urls,  # All URLs for NIP-91
            "name": provider_name,
            "description": description,
            "mint_urls": mint_urls,
            "version": version,
            "content": event.get("content", ""),
        }

    except Exception as e:
        logger.error(
            f"Error parsing provider announcement {event.get('id', 'unknown')}: {e}"
        )
        return None


async def get_cache() -> list[dict[str, Any]]:
    async with _PROVIDERS_CACHE_LOCK:
        return list(_PROVIDERS_CACHE)


def _get_discovery_relays() -> list[str]:
    try:
        relays = settings.relays
    except Exception:
        relays = []
    if not relays:
        relays = [
            "wss://relay.nostr.band",
            "wss://relay.damus.io",
            "wss://relay.routstr.com",
        ]
    return relays


async def _discover_providers(pubkey: str | None = None) -> list[dict[str, Any]]:
    discovery_relays = _get_discovery_relays()

    tasks = [
        query_nostr_relay_for_providers(relay_url=r, pubkey=pubkey, limit=100)
        for r in discovery_relays
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_events: list[dict[str, Any]] = []
    event_ids: set[str] = set()
    for res in results:
        if isinstance(res, BaseException):
            logger.error(f"Relay query failed: {res}")
            continue
        if isinstance(res, list):
            for event in res:
                # Filter out localhost announcements
                try:
                    tags = event.get("tags", [])
                    is_localhost = any(
                        isinstance(tag, list)
                        and len(tag) >= 2
                        and tag[0] == "u"
                        and tag[1] == "http://localhost:8000"
                        for tag in tags
                    )
                    if is_localhost:
                        logger.debug(
                            f"Skipping localhost provider event: {event.get('id', 'unknown')}"
                        )
                        continue
                except Exception:
                    # If tags are malformed, fall through to normal handling
                    pass

                if (eid := event.get("id")) and eid not in event_ids:
                    event_ids.add(eid)
                    all_events.append(event)
        else:
            logger.error(f"Unexpected relay result type: {type(res)}")

    providers: list[dict[str, Any]] = []
    seen_endpoints: set[str] = set()
    for event in all_events:
        parsed = parse_provider_announcement(event)
        if parsed and (eu := parsed.get("endpoint_url")) and eu not in seen_endpoints:
            seen_endpoints.add(eu)
            providers.append(parsed)

    random.shuffle(providers)
    return providers[:42]


async def refresh_providers_cache(pubkey: str | None = None) -> None:
    try:
        providers = await _discover_providers(pubkey=pubkey)

        health_tasks = [
            fetch_provider_health(provider["endpoint_url"]) for provider in providers
        ]
        health_results = await asyncio.gather(*health_tasks, return_exceptions=True)

        new_cache: list[dict[str, Any]] = []
        for provider, hr in zip(providers, health_results):
            if isinstance(hr, Exception):
                health: dict[str, Any] = {
                    "status_code": 500,
                    "endpoint": "error",
                    "json": {"error": str(hr)},
                }
            else:
                health = hr  # type: ignore[assignment]
            new_cache.append({"provider": provider, "health": health})

        async with _PROVIDERS_CACHE_LOCK:
            _PROVIDERS_CACHE.clear()
            _PROVIDERS_CACHE.extend(new_cache)
        logger.info(
            f"Providers cache refreshed with {len(new_cache)} entries (limit 42)"
        )
    except Exception as e:
        logger.error(f"Failed to refresh providers cache: {e}")


async def providers_cache_refresher(
    interval_seconds: int | None = None, pubkey: str | None = None
) -> None:
    if interval_seconds is None:
        try:
            interval_seconds = settings.providers_refresh_interval_seconds
        except Exception:
            interval_seconds = 300

    await refresh_providers_cache(pubkey=pubkey)
    while True:
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            break
        await refresh_providers_cache(pubkey=pubkey)


async def fetch_provider_health(endpoint_url: str) -> dict[str, Any]:
    """Fetch provider health and info, preferring /v1/info for models and pricing."""
    try:
        # Determine if we need Tor proxy based on .onion domain
        is_onion = ".onion" in endpoint_url

        # Set up client arguments conditionally
        proxies = None
        if is_onion:
            try:
                tor_proxy = settings.tor_proxy_url
            except Exception:
                tor_proxy = "socks5://127.0.0.1:9050"
            proxies = {"http://": tor_proxy, "https://": tor_proxy}  # type: ignore[assignment]

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            proxies=proxies,  # type: ignore[arg-type]
        ) as client:
            # Prefer provider's /v1/info for full details
            info_url = f"{endpoint_url.rstrip('/')}/v1/info"
            try:
                response = await client.get(info_url)
                if response.status_code == 200:
                    return {
                        "status_code": response.status_code,
                        "endpoint": "info",
                        "json": response.json(),
                    }
            except Exception:
                pass

            # Fallback to /v1/models
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
    Return cached providers. If include_json, return provider+health; otherwise provider only.
    Optional filter by pubkey.
    """
    cache = await get_cache()
    if not cache:
        await refresh_providers_cache(pubkey=pubkey)
        cache = await get_cache()
    if pubkey:
        cache = [c for c in cache if c.get("provider", {}).get("pubkey") == pubkey]
    if include_json:
        return {"providers": cache}
    providers_only = [c["provider"] for c in cache]
    return {"providers": providers_only}
