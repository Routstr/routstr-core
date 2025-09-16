#!/usr/bin/env python3
"""
NIP-91: Routstr Provider Discoverability Implementation
Automatically announces this Routstr proxy instance to Nostr relays.
"""

import asyncio
import json
import os
import random
import ssl
import time
from typing import Any, cast

from nostr.event import Event
from nostr.filter import Filter, Filters
from nostr.key import PrivateKey
from nostr.message_type import ClientMessageType
from nostr.relay_manager import RelayManager

from .core import get_logger
from .core.settings import settings

logger = get_logger(__name__)


def get_app_version() -> str | None:
    try:
        from .core.main import __version__ as imported_version

        return imported_version
    except Exception:
        return None


def _event_to_dict(ev: Event) -> dict[str, Any]:
    return {
        "id": ev.id,
        "pubkey": ev.public_key,
        "created_at": ev.created_at,
        "kind": int(ev.kind) if not isinstance(ev.kind, int) else ev.kind,
        "tags": ev.tags,
        "content": ev.content,
        "sig": ev.signature,
    }


def nsec_to_keypair(nsec: str) -> tuple[str, str] | None:
    """
    Convert a Nostr private key (nsec) to a keypair (privkey_hex, pubkey_hex).

    Args:
        nsec: Nostr private key in nsec format or hex format

    Returns:
        Tuple of (private_key_hex, public_key_hex) or None if invalid
    """
    try:
        if nsec.startswith("nsec"):
            pk = PrivateKey.from_nsec(nsec)
            return (pk.hex(), pk.public_key.hex())

        if len(nsec) == 64:
            pk = PrivateKey(bytes.fromhex(nsec))
            return (pk.hex(), pk.public_key.hex())

        logger.error(f"Invalid private key format/length: {len(nsec)}")
        return None
    except Exception as e:
        logger.error(f"Failed to convert nsec to keypair: {e}")
        return None


def create_nip91_event(
    private_key_hex: str,
    provider_id: str,
    endpoint_urls: list[str],
    mint_urls: list[str] | None = None,
    version: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Create a NIP-91 compliant provider announcement event (kind:38421).

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
    pk = PrivateKey(bytes.fromhex(private_key_hex))

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

    ev = Event(pk.public_key.hex(), content, kind=38421, tags=tags)
    pk.sign_event(ev)
    return _event_to_dict(ev)


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


async def query_nip91_events(
    relay_url: str,
    pubkey: str,
    provider_id: str | None = None,
    timeout: int = 30,
) -> tuple[list[dict[str, Any]], bool]:
    """
    Query a Nostr relay for NIP-91 provider announcements (kind:38421) via nostr library.

    Returns a tuple of (events, ok) where ok indicates whether the relay interaction
    succeeded without transport-level errors.
    """

    def _sync_query() -> tuple[list[dict[str, Any]], bool]:
        rm = RelayManager()
        rm.add_relay(relay_url)
        events_out: list[dict[str, Any]] = []
        ok = True
        try:
            rm.open_connections({"cert_reqs": ssl.CERT_NONE})
            time.sleep(1.0)

            flt = Filter(kinds=[38421], authors=[pubkey], limit=10)
            filters = Filters([flt])
            sub_id = f"nip91_{int(time.time())}"
            rm.add_subscription(sub_id, filters)
            req: list[Any] = [ClientMessageType.REQUEST, sub_id]
            req.extend(filters.to_json_array())
            rm.publish_message(json.dumps(req))

            start = time.time()
            last_event_ts = start
            while time.time() - start < timeout:
                drained = False
                while rm.message_pool.has_events():
                    drained = True
                    ev_msg = rm.message_pool.get_event()
                    ev = ev_msg.event
                    ev_dict = _event_to_dict(ev)
                    if provider_id is not None:
                        tags = ev_dict.get("tags", [])
                        if not any(
                            isinstance(t, list)
                            and len(t) >= 2
                            and t[0] == "d"
                            and t[1] == provider_id
                            for t in tags
                        ):
                            continue
                    events_out.append(ev_dict)
                    logger.debug(
                        f"Found existing NIP-91 event: {ev_dict.get('id', '')}"
                    )
                if drained:
                    last_event_ts = time.time()

                while rm.message_pool.has_notices():
                    notice = rm.message_pool.get_notice()
                    try:
                        content = getattr(notice, "content", notice)
                        s = str(content)
                        if len(s) > 200:
                            s = s[:200] + "..."
                        logger.debug(f"Relay notice: {s}")
                    except Exception:
                        pass

                if time.time() - last_event_ts > 2.5:
                    break

                time.sleep(0.1)
        except Exception as e:
            ok = False
            logger.debug(f"Failed to query relay {relay_url}: {type(e).__name__}")
        finally:
            try:
                rm.close_connections()
            except Exception:
                pass
        return events_out, ok

    return await asyncio.to_thread(_sync_query)


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
            events, _ok = await query_nip91_events(relay_url, public_key_hex, None)
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
    Publish a NIP-91 event to a nostr relay via nostr library.
    """

    def _sync_publish() -> bool:
        rm = RelayManager()
        rm.add_relay(relay_url)
        try:
            rm.open_connections({"cert_reqs": ssl.CERT_NONE})
            time.sleep(1.0)
            # Publish the event as-is via publish_message to preserve signature
            rm.publish_message(json.dumps(["EVENT", event]))
            logger.debug(f"Sent NIP-91 event {event.get('id', '')} to {relay_url}")
            time.sleep(1.0)
            return True
        except Exception as e:
            logger.debug(f"Failed to publish to {relay_url}: {type(e).__name__}")
            return False
        finally:
            try:
                rm.close_connections()
            except Exception:
                pass

    return await asyncio.to_thread(_sync_publish)


async def announce_provider() -> None:
    """
    Background task to announce this Routstr provider to Nostr relays.
    Checks for existing announcements and creates new ones if needed.
    """
    # Check for NSEC in environment (use NSEC only)
    nsec = settings.nsec
    if not nsec:
        logger.info("Nostr private key not found (NSEC), skipping NIP-91 announcement")
        return

    # Convert NSEC to keypair
    keypair = nsec_to_keypair(nsec)
    if not keypair:
        logger.error("Failed to parse NSEC, skipping NIP-91 announcement")
        return

    private_key_hex, public_key_hex = keypair
    logger.info(f"Using Nostr pubkey: {public_key_hex}")

    # Resolve settings and determine if we can publish BEFORE touching relays
    try:
        base_url: str | None = settings.http_url
        onion_url: str | None = settings.onion_url
        provider_name = settings.name or "Routstr Proxy"
        provider_about = settings.description or "Privacy-preserving AI proxy via Nostr"
        cashu_mints = [m.strip() for m in settings.cashu_mints if m.strip()]
    except Exception:
        base_url = settings.http_url or None
        onion_url = settings.onion_url or None
        provider_name = settings.name or "Routstr Proxy"
        provider_about = settings.description or "Privacy-preserving AI proxy via Nostr"
        cashu_mints = [m.strip() for m in settings.cashu_mints if m.strip()]
    if not onion_url:
        discovered = discover_onion_url_from_tor()
        if discovered:
            onion_url = discovered
            logger.info(f"Discovered onion URL via Tor volume: {onion_url}")
    mint_urls = cashu_mints if cashu_mints else None

    endpoint_urls: list[str] = []
    if base_url and base_url.strip() and base_url.strip() != "http://localhost:8000":
        endpoint_urls.append(base_url.strip())
    if onion_url and onion_url.strip():
        ou = onion_url.strip()
        if ou.endswith(".onion") and not (
            ou.startswith("http://") or ou.startswith("https://")
        ):
            ou = f"http://{ou}"
        endpoint_urls.append(ou)

    if not endpoint_urls:
        logger.warning(
            "No valid endpoints configured (HTTP_URL/ONION_URL). Skipping NIP-91 publish."
        )
        return

    # Only now configure relays and determine provider_id (may query relays)
    relay_urls = [u.strip() for u in getattr(settings, "relays", []) if u.strip()]
    if not relay_urls:
        relay_urls = [
            "wss://relay.nostr.band",
            "wss://relay.damus.io",
            "wss://relay.routstr.com",
            "wss://nos.lol",
        ]

    provider_id = await _determine_provider_id(public_key_hex, relay_urls)
    logger.info(f"Using provider_id: {provider_id}")

    # Build metadata
    metadata = {
        "name": provider_name,
        "about": provider_about,
    }

    # Create the candidate event that we would publish
    version_str = get_app_version()
    candidate_event = create_nip91_event(
        private_key_hex=private_key_hex,
        provider_id=provider_id,
        endpoint_urls=endpoint_urls,
        mint_urls=mint_urls,
        version=version_str,
        metadata=metadata,
    )

    # Backoff configuration and state (sensible defaults)
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

    # Fetch existing events for this provider_id
    existing_events: list[dict[str, Any]] = []
    for relay_url in relay_urls:
        if _should_skip(relay_url):
            logger.debug(f"Skipping {relay_url} due to backoff")
            continue
        events, ok = await query_nip91_events(relay_url, public_key_hex, provider_id)
        if ok:
            _register_success(relay_url)
            existing_events.extend(events)
        else:
            _register_failure(relay_url)

    # Decide whether to publish: publish if none exist or any differ from candidate
    found_any = len(existing_events) > 0
    all_match = found_any and all(
        events_semantically_equal(ev, candidate_event) for ev in existing_events
    )

    if not all_match:
        logger.debug(
            "No matching NIP-91 announcement found or differences detected; publishing update"
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
            f"Published NIP-91 announcement to {success_count}/{len(relay_urls)} relays"
        )
    else:
        logger.debug(
            "Matching NIP-91 announcement already present; skipping publish on startup"
        )

    # Re-announce periodically (every 24 hours)
    announcement_interval = 24 * 60 * 60

    while True:
        try:
            await asyncio.sleep(announcement_interval)

            # Build fresh candidate event for comparison
            version_str = get_app_version()
            candidate_event = create_nip91_event(
                private_key_hex=private_key_hex,
                provider_id=provider_id,
                endpoint_urls=endpoint_urls,
                mint_urls=mint_urls,
                version=version_str,
                metadata=metadata,
            )

            # Fetch existing events for this provider_id
            existing_events = []
            for relay_url in relay_urls:
                if _should_skip(relay_url):
                    logger.debug(f"Skipping {relay_url} due to backoff")
                    continue
                events, ok = await query_nip91_events(
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
                    "Matching NIP-91 announcement already present; skipping periodic re-announce"
                )
                continue

            logger.debug(
                f"Re-announcing provider due to differences or absence: {candidate_event['id']}"
            )
            for relay_url in relay_urls:
                if _should_skip(relay_url):
                    logger.debug(f"Skipping publish to {relay_url} due to backoff")
                    continue
                ok = await publish_to_relay(relay_url, candidate_event)
                if ok:
                    _register_success(relay_url)
                else:
                    _register_failure(relay_url)

        except asyncio.CancelledError:
            logger.info("NIP-91 announcement task cancelled")
            break
        except Exception as e:
            logger.debug(f"Error in NIP-91 announcement loop: {type(e).__name__}")
            # Continue running despite errors
