#!/usr/bin/env python3
"""
Listing: Routstr Provider Discoverability Implementation
Automatically announces this Routstr proxy instance to Nostr relays.
"""

import asyncio
import json
import os
import random
import time
from typing import Any, cast

from ..core import get_logger
from ..core.settings import settings
from .sdk import create_signed_event, fetch_events, parse_keypair, send_event

logger = get_logger(__name__)


def get_app_version() -> str | None:
    try:
        from ..core.version import __version__ as imported_version

        return imported_version
    except Exception:
        return None


def nsec_to_keypair(nsec: str) -> tuple[str, str] | None:
    """
    Convert a Nostr private key (nsec) to a keypair (privkey_hex, pubkey_hex).

    Args:
        nsec: Nostr private key in nsec format or hex format

    Returns:
        Tuple of (private_key_hex, public_key_hex) or None if invalid
    """
    try:
        if not (nsec.startswith("nsec") or len(nsec) == 64):
            logger.error(f"Invalid private key format/length: {len(nsec)}")
            return None
        return parse_keypair(nsec)
    except Exception as e:
        logger.error(f"Failed to convert nsec to keypair: {e}")
        return None


def create_listing_event(
    private_key_hex: str,
    provider_id: str,
    endpoint_urls: list[str],
    mint_urls: list[str] | None = None,
    version: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Create a listing provider announcement event (kind:38421).

    Args:
        private_key_hex: 32-byte hex private key for signing
        provider_id: Unique identifier for this provider (d tag)
        endpoint_urls: List of URLs to connect to the provider
        mint_urls: Optional list of ecash mint URLs for payments
        version: Provider software version
        metadata: Optional metadata dictionary (name, picture, about, etc.)

    Returns:
        Complete signed nostr event as a dict ready for publishing
    """
    tags = [["d", provider_id]]
    for url in endpoint_urls:
        tags.append(["u", url])
    if mint_urls:
        for m in mint_urls:
            if m:
                tags.append(["mint", m])
    if version:
        tags.append(["version", version])

    content = json.dumps(metadata, separators=(",", ":")) if metadata else ""

    return create_signed_event(
        private_key_hex,
        kind=38421,
        content=content,
        tags=tags,
    )


def _get_tag_values(event: dict[str, Any], key: str) -> list[str]:
    tags = event.get("tags", [])
    values: list[str] = []
    for tag in tags:
        if isinstance(tag, list) and tag and tag[0] == key and len(tag) >= 2:
            values.append(tag[1])
    return values


def _get_single_tag_value(event: dict[str, Any], key: str) -> str | None:
    values = _get_tag_values(event, key)
    return values[0] if values else None


def _parse_content_json(content: str) -> dict[str, Any]:
    if not content:
        return {}
    try:
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def events_semantically_equal(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a.get("kind") != b.get("kind"):
        return False

    if _get_single_tag_value(a, "d") != _get_single_tag_value(b, "d"):
        return False

    urls_a = set(_get_tag_values(a, "u"))
    urls_b = set(_get_tag_values(b, "u"))
    if urls_a != urls_b:
        return False

    mints_a = set(_get_tag_values(a, "mint"))
    mints_b = set(_get_tag_values(b, "mint"))
    if mints_a != mints_b:
        return False

    if _get_single_tag_value(a, "version") != _get_single_tag_value(b, "version"):
        return False

    content_a = _parse_content_json(cast(str, a.get("content", "")))
    content_b = _parse_content_json(cast(str, b.get("content", "")))
    if content_a != content_b:
        return False

    return True


async def query_listing_events(
    relay_url: str,
    pubkey: str,
    provider_id: str | None = None,
    timeout: int = 30,
) -> tuple[list[dict[str, Any]], bool]:
    """
    Query a Nostr relay for listing provider announcements (kind:38421) via nostr library.

    Returns a tuple of (events, ok) where ok indicates whether the relay interaction
    succeeded without transport-level errors.
    """

    try:
        events_out = await fetch_events(
            relay_url,
            kind=38421,
            author=pubkey,
            limit=10,
            timeout=timeout,
        )
    except Exception as e:
        logger.debug(f"Failed to query relay {relay_url}: {type(e).__name__}")
        return [], False

    if provider_id is not None:
        events_out = [
            event
            for event in events_out
            if _get_single_tag_value(event, "d") == provider_id
        ]
    return events_out, True


def discover_onion_url_from_tor(base_dir: str = "/var/lib/tor") -> str | None:
    """Discover onion URL by reading Tor hidden service hostname files.

    Tries common paths first, then scans recursively for any 'hostname' file.
    Returns an http URL like 'http://<host>.onion' if found.
    """
    common_candidates = [
        os.path.join(base_dir, "hs", "router", "hostname"),
        os.path.join(base_dir, "hs", "ROUTER", "hostname"),
        os.path.join(base_dir, "hidden_service", "hostname"),
    ]

    for candidate in common_candidates:
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                host = f.readline().strip()
            if host and host.endswith(".onion"):
                return f"http://{host}"
        except Exception:
            pass

    try:
        for root, _dirs, files in os.walk(base_dir):
            if "hostname" in files:
                path = os.path.join(root, "hostname")
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        host = f.readline().strip()
                    if host and host.endswith(".onion"):
                        return f"http://{host}"
                except Exception:
                    continue
    except Exception:
        pass

    return None


async def _determine_provider_id(public_key_hex: str, relay_urls: list[str]) -> str:
    explicit = settings.provider_id
    if explicit:
        logger.info(f"Using configured provider_id from env: {explicit}")
        return explicit

    async def query_single_relay(relay_url: str) -> list[dict[str, Any]]:
        try:
            events, _ok = await query_listing_events(relay_url, public_key_hex, None)
            return events
        except Exception:
            return []

    # Query all relays concurrently
    all_events_lists = await asyncio.gather(
        *[query_single_relay(relay_url) for relay_url in relay_urls]
    )

    latest_event: dict[str, Any] | None = None
    latest_ts = -1

    for events_list in all_events_lists:
        for ev in events_list:
            ts = int(ev.get("created_at", 0))
            if ts > latest_ts:
                latest_event = ev
                latest_ts = ts

    existing_d = _get_single_tag_value(latest_event, "d") if latest_event else None
    if existing_d:
        logger.info(f"Reusing existing provider_id from relay: {existing_d}")
        return existing_d

    fallback = public_key_hex[:12]
    logger.info(f"No existing provider_id found; using fallback: {fallback}")
    return fallback


async def publish_to_relay(
    relay_url: str,
    event: dict[str, Any],
    timeout: int = 30,
) -> bool:
    """
    Publish a listing event to a nostr relay via nostr library.
    """

    try:
        await send_event(relay_url, event, timeout=timeout)
        logger.debug(f"Sent listing event {event.get('id', '')} to {relay_url}")
        return True
    except Exception as e:
        logger.debug(f"Failed to publish to {relay_url}: {type(e).__name__}")
        return False


# Re-announce cadence once a provider is listed.
ANNOUNCEMENT_INTERVAL_SECONDS = 24 * 60 * 60
# Poll cadence while there is nothing to announce (no NSEC, no endpoint, ...).
DISABLED_POLL_SECONDS = 60
# How often the long re-announce sleep re-checks the configured NSEC, so a
# newly saved identity is announced promptly instead of up to 24h later.
IDENTITY_POLL_SECONDS = 30

DEFAULT_RELAY_URLS = [
    "wss://relay.nostr.band",
    "wss://relay.damus.io",
    "wss://relay.routstr.com",
    "wss://nos.lol",
]


def _resolve_endpoint_urls() -> list[str]:
    """Endpoints to advertise: a public HTTP URL and/or an onion URL."""
    endpoint_urls: list[str] = []

    base_url = (settings.http_url or "").strip()
    if base_url and base_url != "http://localhost:8000":
        endpoint_urls.append(base_url)

    onion_url = (settings.onion_url or "").strip()
    if not onion_url:
        discovered = discover_onion_url_from_tor()
        if discovered:
            onion_url = discovered
            logger.info(f"Discovered onion URL via Tor volume: {onion_url}")

    if onion_url:
        if onion_url.endswith(".onion") and not (
            onion_url.startswith("http://") or onion_url.startswith("https://")
        ):
            onion_url = f"http://{onion_url}"
        endpoint_urls.append(onion_url)

    return endpoint_urls


def _resolve_relay_urls() -> list[str]:
    relay_urls = [u.strip() for u in getattr(settings, "relays", []) if u.strip()]
    return relay_urls or list(DEFAULT_RELAY_URLS)


def _resolve_mint_urls() -> list[str] | None:
    mints = [m.strip() for m in (settings.cashu_mints or []) if m.strip()]
    return mints or None


async def _sleep_until_next_announcement(
    seconds: float, parsed_nsec: str | None
) -> None:
    """Sleep up to ``seconds``, returning early if the configured NSEC changes.

    Without the early wake, a node runner who replaces the NSEC in the admin UI
    would wait out the whole re-announce interval before the new identity (and,
    with it, the new ``d`` tag and npub) is announced.
    """
    remaining = float(seconds)
    while remaining > 0:
        if (settings.nsec or "").strip() != (parsed_nsec or ""):
            return
        chunk = min(float(IDENTITY_POLL_SECONDS), remaining)
        await asyncio.sleep(chunk)
        remaining -= chunk


async def announce_provider() -> None:
    """Background task announcing this Routstr provider to Nostr relays.

    Started unconditionally at boot: while the node has no NSEC the task idles
    and re-checks, so an identity configured later through the admin UI is
    picked up (and announced) without a restart. The identity, endpoints, mints
    and relays are all re-read every iteration, mirroring
    ``publish_usage_analytics``.
    """
    parsed_nsec: str | None = None
    private_key_hex: str | None = None
    public_key_hex: str | None = None
    provider_id: str | None = None
    warned_missing_nsec = False

    # Backoff state is deliberately long-lived: it has to survive an idle poll,
    # a full re-announce cycle and an identity change, otherwise a failing relay
    # would be retried at full rate on every pass.
    backoff_base = 5.0
    backoff_max = 900.0
    backoff_jitter_ratio = 0.2
    relay_next_allowed: dict[str, float] = {}
    relay_current_delay: dict[str, float] = {}

    def _should_skip(relay: str) -> bool:
        return time.time() < relay_next_allowed.get(relay, 0.0)

    def _register_success(relay: str) -> None:
        relay_current_delay[relay] = 0.0
        relay_next_allowed[relay] = time.time()

    def _register_failure(relay: str) -> None:
        previous = relay_current_delay.get(relay, 0.0)
        delay = backoff_base if previous <= 0.0 else min(backoff_max, previous * 2.0)
        jitter = delay * backoff_jitter_ratio * (2.0 * random.random() - 1.0)
        scheduled = time.time() + max(0.0, delay + jitter)
        relay_current_delay[relay] = delay
        relay_next_allowed[relay] = scheduled
        logger.debug(
            f"Backoff: {relay} delay={delay:.1f}s jitter={jitter:.1f}s next={int(scheduled)}"
        )

    while True:
        try:
            nsec = (settings.nsec or "").strip()

            if not nsec:
                if not warned_missing_nsec:
                    logger.info(
                        "Nostr private key not configured (NSEC); waiting for one "
                        "to be set before announcing this provider"
                    )
                    warned_missing_nsec = True
                parsed_nsec = None
                await asyncio.sleep(DISABLED_POLL_SECONDS)
                continue

            # Re-derive the identity whenever the configured NSEC changes, so a
            # key saved (or replaced) through the admin UI takes effect live.
            if nsec != parsed_nsec:
                keypair = nsec_to_keypair(nsec)
                if not keypair:
                    logger.error(
                        "Invalid NSEC; waiting for a valid one before announcing"
                    )
                    parsed_nsec = None
                    await asyncio.sleep(DISABLED_POLL_SECONDS)
                    continue
                private_key_hex, public_key_hex = keypair
                parsed_nsec = nsec
                provider_id = None
                warned_missing_nsec = False
                logger.info(f"Using Nostr pubkey: {public_key_hex}")

            if private_key_hex is None or public_key_hex is None:
                await asyncio.sleep(DISABLED_POLL_SECONDS)
                continue

            endpoint_urls = _resolve_endpoint_urls()
            if not endpoint_urls:
                logger.warning(
                    "No valid endpoints configured (HTTP_URL/ONION_URL). "
                    "Skipping listing publish until one is set."
                )
                await asyncio.sleep(DISABLED_POLL_SECONDS)
                continue

            relay_urls = _resolve_relay_urls()

            if provider_id is None:
                provider_id = await _determine_provider_id(public_key_hex, relay_urls)
                logger.info(f"Using provider_id: {provider_id}")

            metadata = {
                "name": settings.name or "Routstr Proxy",
                "about": settings.description
                or "Privacy-preserving AI proxy via Nostr",
            }

            candidate_event = create_listing_event(
                private_key_hex=private_key_hex,
                provider_id=provider_id,
                endpoint_urls=endpoint_urls,
                mint_urls=_resolve_mint_urls(),
                version=get_app_version(),
                metadata=metadata,
            )

            # Fetch existing events for this provider_id
            existing_events: list[dict[str, Any]] = []
            for relay_url in relay_urls:
                if _should_skip(relay_url):
                    logger.debug(f"Skipping {relay_url} due to backoff")
                    continue
                events, ok = await query_listing_events(
                    relay_url, public_key_hex, provider_id
                )
                if ok:
                    _register_success(relay_url)
                    existing_events.extend(events)
                else:
                    _register_failure(relay_url)

            found_any = len(existing_events) > 0
            all_match = found_any and all(
                events_semantically_equal(ev, candidate_event) for ev in existing_events
            )

            if all_match:
                logger.debug(
                    "Matching listing announcement already present; skipping publish"
                )
            else:
                logger.debug(
                    "No matching listing announcement found or differences "
                    "detected; publishing update"
                )
                success_count = 0
                for relay_url in relay_urls:
                    if _should_skip(relay_url):
                        logger.debug(f"Skipping publish to {relay_url} due to backoff")
                        continue
                    if await publish_to_relay(relay_url, candidate_event):
                        _register_success(relay_url)
                        success_count += 1
                    else:
                        _register_failure(relay_url)
                logger.info(
                    "Published listing announcement to "
                    f"{success_count}/{len(relay_urls)} relays"
                )

            # Re-announce periodically; wakes early if the NSEC changes.
            await _sleep_until_next_announcement(
                ANNOUNCEMENT_INTERVAL_SECONDS, parsed_nsec
            )

        except asyncio.CancelledError:
            logger.info("Listing announcement task cancelled")
            break
        except Exception as e:
            logger.debug(f"Error in listing announcement loop: {type(e).__name__}")
            await asyncio.sleep(DISABLED_POLL_SECONDS)
