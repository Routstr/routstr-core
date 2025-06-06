from fastapi import APIRouter
import asyncio
import json
import websockets
import random
import string
import re
import httpx
import os
import logging

providers_router = APIRouter(prefix="/v1/providers")

logger = logging.getLogger(__name__)


def generate_subscription_id() -> str:
    """Generate a random subscription ID."""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=10))


def extract_onion_urls(content: str) -> list[str]:
    """Extract onion URLs from content."""
    pattern = r"http?://[a-zA-Z0-9\-._~]+\.onion"
    return re.findall(pattern, content)


async def query_nostr_relay_with_search(
    search_term: str,
    relay_url: str,
    kinds: list[int] | None = None,
    limit: int = 1000,
    timeout: int = 30,
) -> list[dict]:
    """
    Query a Nostr relay and filter for events containing a search term.
    """
    if kinds is None:
        kinds = [1]

    events = []

    # If searching for an npub mention, try tag-based search first
    if search_term.startswith("nostr:npub"):
        # Extract the npub and convert to hex
        npub = search_term.replace("nostr:", "")
        try:
            # Convert npub to hex (you might need to implement or import this)
            # For now, try tag-based search with the npub
            filter_obj = {
                "kinds": kinds,
                "limit": limit,
                "#p": [npub],  # Posts that tag this pubkey
            }
        except Exception:
            # If conversion fails, try regular search
            filter_obj = {
                "kinds": kinds,
                "limit": limit,
            }
    else:
        # Try relay's search functionality (NIP-50)
        filter_obj = {
            "kinds": kinds,
            "search": search_term,
            "limit": limit,
        }

    sub_id = generate_subscription_id()
    req_message = json.dumps(["REQ", sub_id, filter_obj])

    try:
        async with websockets.connect(relay_url, timeout=timeout) as websocket:
            logger.info("Connected to relay, sending request with filter: %s", filter_obj)
            await websocket.send(req_message)

            while True:
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=5)
                    data = json.loads(message)

                    if data[0] == "EVENT" and data[1] == sub_id:
                        # For tag-based search, also check content
                        if search_term.startswith("nostr:npub"):
                            if search_term.lower() in data[2]["content"].lower():
                                logger.info("Found matching event: %s", data[2]['id'])
                                events.append(data[2])
                        else:
                            logger.info("Found matching event: %s", data[2]['id'])
                            events.append(data[2])
                    elif data[0] == "EOSE" and data[1] == sub_id:
                        logger.info("Received EOSE message")
                        break
                    elif data[0] == "NOTICE":
                        logger.info("Relay notice: %s", data[1])
                        # If search not supported, could break and try different approach
                        if "unrecognised filter item" in data[1] and "search" in str(
                            filter_obj
                        ):
                            logger.info("Search not supported on this relay")
                            break

                except asyncio.TimeoutError:
                    logger.warning("Timeout waiting for message")
                    break
                except json.JSONDecodeError:
                    logger.error("Failed to decode message as JSON")
                    continue

            await websocket.send(json.dumps(["CLOSE", sub_id]))

    except Exception as e:
        logger.error("Query failed: %s", e)

    logger.info("Query complete. Found %s matching events", len(events))
    return events


async def get_cache() -> list[dict]:
    return []  # TODO: Implement cache


async def fetch_onion(provider: str) -> dict:
    """Check if an onion service is healthy by making a GET request to its root."""
    try:
        # Get Tor proxy URL from environment variable, default to local Tor SOCKS5 proxy
        tor_proxy = os.getenv("TOR_PROXY_URL", "socks5://127.0.0.1:9050")

        # Configure httpx to use Tor SOCKS5 proxy
        async with httpx.AsyncClient(
            proxies={"http://": tor_proxy, "https://": tor_proxy},  # type: ignore
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        ) as client:
            response = await client.get(provider)
            # Consider 2xx and 3xx status codes as healthy
            return {"status_code": response.status_code, "json": response.json()}
    except Exception:
        # Any exception means the service is not healthy
        return {"status_code": 500, "json": {"error": "Failed to fetch onion"}}


@providers_router.get("/")
async def get_providers(include_json: bool = False):
    npub = "npub130mznv74rxs032peqym6g3wqavh472623mt3z5w73xq9r6qqdufs7ql29s"

    # Relays that support NIP-50 text search
    search_relays = [
        "wss://relay.nostr.band",  # Known to support search
        "wss://nostr.wine",  # Known to support search
        "wss://relay.damus.io",
        "wss://nos.lol",
    ]

    # Search for the mention format that appears in posts
    search_term = f"nostr:{npub}"

    all_events = []
    event_ids = set()  # To avoid duplicates

    # Try multiple relays
    for relay_url in search_relays:
        logger.info("\nTrying relay: %s", relay_url)
        try:
            events = await query_nostr_relay_with_search(
                search_term=search_term,
                relay_url=relay_url,
                kinds=[1],  # Text notes
                limit=500,
            )

            # Add unique events
            for event in events:
                if event["id"] not in event_ids:
                    event_ids.add(event["id"])
                    all_events.append(event)

            logger.info("Got %s events from %s", len(events), relay_url)

            # If we have enough events, we can stop
            if len(all_events) >= 100:
                break

        except Exception as e:
            logger.error("Failed to query %s: %s", relay_url, e)
            continue

    logger.info("Found %s total unique events mentioning routstr", len(all_events))

    providers = []
    for event in all_events:
        onion_urls = extract_onion_urls(event["content"])
        providers.extend(onion_urls)

    unique_providers = list(set(providers))

    logger.info("Found %s unique onion URLs", len(unique_providers))
    logger.info("%s", unique_providers)

    healthy_providers: list[dict | str] = []
    for provider in unique_providers:
        response = await fetch_onion(provider)

        if include_json:
            healthy_providers.append({provider: response["json"]})
        else:
            healthy_providers.append(provider)

    return {"providers": healthy_providers}
