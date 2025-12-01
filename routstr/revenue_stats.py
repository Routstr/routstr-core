import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from nostr.event import Event
from nostr.key import PrivateKey

from .core.log_manager import log_manager
from .core.logging import get_logger
from .core.settings import settings
from .nip91 import nsec_to_keypair, publish_to_relay

logger = get_logger(__name__)

# Custom kind for Revenue Stats
KIND_REVENUE_STATS = 7375


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


def create_revenue_event(
    private_key_hex: str, stats: dict[str, Any], date_str: str
) -> dict[str, Any]:
    pk = PrivateKey(bytes.fromhex(private_key_hex))

    # Content is the stats JSON
    content = json.dumps(stats, separators=(",", ":"))

    # Tags:
    # d: revenue-YYYY-MM-DD (to make it unique per day if replaceable)
    # t: revenue
    # date: YYYY-MM-DD
    tags = [["d", f"revenue-{date_str}"], ["t", "revenue"], ["date", date_str]]

    ev = Event(pk.public_key.hex(), content, kind=KIND_REVENUE_STATS, tags=tags)
    pk.sign_event(ev)
    return _event_to_dict(ev)


async def publish_revenue_stats_task() -> None:
    """
    Background task that runs once a day (at midnight UTC) to publish revenue stats.
    """
    logger.info("Revenue stats publisher task started")

    while True:
        try:
            # Calculate time until next midnight UTC
            now = datetime.now(timezone.utc)
            next_run = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            sleep_seconds = (next_run - now).total_seconds()

            logger.info(
                f"Revenue stats task sleeping for {sleep_seconds:.1f}s until {next_run}"
            )
            await asyncio.sleep(sleep_seconds)

            # Check if enabled
            if not settings.enable_revenue_stats_publishing:
                logger.info("Revenue stats publishing disabled, skipping")
                continue

            # It's midnight! Fetch stats for the PREVIOUS day (last 24h)
            nsec = settings.nsec
            if not nsec:
                logger.warning("No NSEC configured, skipping revenue stats publish")
                continue

            keypair = nsec_to_keypair(nsec)
            if not keypair:
                logger.error("Invalid NSEC, skipping revenue stats publish")
                continue

            private_key_hex, _ = keypair

            # Get date string for the day that just finished
            yesterday = datetime.now(timezone.utc) - timedelta(days=1)
            date_str = yesterday.strftime("%Y-%m-%d")

            logger.info(f"Publishing revenue stats for {date_str}")

            stats = log_manager.get_usage_summary(hours=24)

            # Create event
            event = create_revenue_event(private_key_hex, stats, date_str)

            # Publish to relays
            relay_urls = [u.strip() for u in settings.relays if u.strip()]
            if not relay_urls:
                relay_urls = [
                    "wss://relay.nostr.band",
                    "wss://relay.damus.io",
                    "wss://relay.routstr.com",
                    "wss://nos.lol",
                ]

            success_count = 0
            for relay in relay_urls:
                if await publish_to_relay(relay, event):
                    success_count += 1

            logger.info(
                f"Published revenue stats to {success_count}/{len(relay_urls)} relays"
            )

            # Sleep a bit to avoid rapid loop if clock skews back
            await asyncio.sleep(60)

        except asyncio.CancelledError:
            logger.info("Revenue stats task cancelled")
            break
        except Exception as e:
            logger.error(f"Error in revenue stats task: {e}")
            # Sleep 1 hour before retrying loop calculation to avoid busy loop on error
            await asyncio.sleep(3600)
