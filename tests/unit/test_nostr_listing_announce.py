"""Tests for the kind 38421 provider announcement loop.

The regression these cover: the node runner configures the NSEC through the
admin UI (not the ``.env`` file), which only mutates the live ``settings``
singleton. The announcement task must therefore (a) already be running, and
(b) pick the new identity up on its own — without a process restart.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from routstr.nostr import listing

NSEC_A = "11" * 32
NSEC_B = "22" * 32


def _quiet_settings(monkeypatch: Any, nsec: str = "") -> None:
    """Pin the settings the loop reads, and keep it off the network/Tor."""
    monkeypatch.setattr(listing.settings, "nsec", nsec)
    monkeypatch.setattr(listing.settings, "http_url", "https://node.example.com")
    monkeypatch.setattr(listing.settings, "onion_url", "")
    monkeypatch.setattr(listing.settings, "relays", [])
    monkeypatch.setattr(listing.settings, "provider_id", "testprovider")
    monkeypatch.setattr(listing.settings, "cashu_mints", [])
    monkeypatch.setattr(listing, "discover_onion_url_from_tor", lambda: None)


def _capture_publishes(monkeypatch: Any) -> list[dict[str, Any]]:
    published: list[dict[str, Any]] = []

    async def fake_query(
        *args: Any, **kwargs: Any
    ) -> tuple[list[dict[str, Any]], bool]:
        return [], True

    async def fake_publish(
        relay_url: str, event: dict[str, Any], timeout: int = 30
    ) -> bool:
        published.append(event)
        return True

    monkeypatch.setattr(listing, "query_listing_events", fake_query)
    monkeypatch.setattr(listing, "publish_to_relay", fake_publish)
    return published


def _distinct_events(published: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per announcement; the publisher is invoked once per relay."""
    by_id: dict[str, dict[str, Any]] = {}
    for event in published:
        by_id[event["id"]] = event
    return list(by_id.values())


@pytest.mark.asyncio
async def test_announce_provider_idles_without_nsec_then_publishes_when_saved(
    monkeypatch: Any,
) -> None:
    """The reported bug: NSEC saved through the admin UI must get announced."""
    sleeps: list[float] = []
    published = _capture_publishes(monkeypatch)
    _quiet_settings(monkeypatch, nsec="")

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 1:
            # The node runner hits "save" in the admin UI: the nsec appears on
            # the live settings singleton while the task is already idling.
            monkeypatch.setattr(listing.settings, "nsec", NSEC_A)
            return
        raise asyncio.CancelledError()

    monkeypatch.setattr(listing.asyncio, "sleep", fake_sleep)

    await listing.announce_provider()

    # First pass idles (no nsec), then the announcing pass sleeps one poll tick
    # into the re-announce interval.
    assert sleeps == [
        listing.DISABLED_POLL_SECONDS,
        listing.IDENTITY_POLL_SECONDS,
    ]
    announcements = _distinct_events(published)
    assert len(announcements) == 1
    assert announcements[0]["kind"] == 38421


@pytest.mark.asyncio
async def test_announce_provider_reannounces_when_nsec_is_replaced(
    monkeypatch: Any,
) -> None:
    """Replacing the key must re-resolve the identity and announce again."""
    sleeps: list[float] = []
    published = _capture_publishes(monkeypatch)
    _quiet_settings(monkeypatch, nsec=NSEC_A)

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 1:
            monkeypatch.setattr(listing.settings, "nsec", NSEC_B)
            return
        raise asyncio.CancelledError()

    monkeypatch.setattr(listing.asyncio, "sleep", fake_sleep)

    await listing.announce_provider()

    assert len(published) == 2 * len(listing.DEFAULT_RELAY_URLS)
    announcements = _distinct_events(published)
    assert len(announcements) == 2
    pubkeys = {event["pubkey"] for event in announcements}
    assert len(pubkeys) == 2, "each identity must be announced under its own pubkey"


@pytest.mark.asyncio
async def test_announce_provider_idles_without_endpoints(monkeypatch: Any) -> None:
    """No publishable endpoint: idle instead of exiting, and never publish."""
    sleeps: list[float] = []
    published = _capture_publishes(monkeypatch)
    _quiet_settings(monkeypatch, nsec=NSEC_A)
    monkeypatch.setattr(listing.settings, "http_url", "")

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise asyncio.CancelledError()

    monkeypatch.setattr(listing.asyncio, "sleep", fake_sleep)

    await listing.announce_provider()

    assert published == []
    assert sleeps == [listing.DISABLED_POLL_SECONDS]


@pytest.mark.asyncio
async def test_sleep_until_next_announcement_wakes_on_nsec_change(
    monkeypatch: Any,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(listing.settings, "nsec", NSEC_A)

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        monkeypatch.setattr(listing.settings, "nsec", NSEC_B)

    monkeypatch.setattr(listing.asyncio, "sleep", fake_sleep)

    await listing._sleep_until_next_announcement(
        listing.ANNOUNCEMENT_INTERVAL_SECONDS, NSEC_A
    )

    # Woke on the first poll tick rather than sleeping out the whole interval.
    assert sleeps == [listing.IDENTITY_POLL_SECONDS]


@pytest.mark.asyncio
async def test_sleep_until_next_announcement_buckets_a_short_interval(
    monkeypatch: Any,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(listing.settings, "nsec", NSEC_A)

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(listing.asyncio, "sleep", fake_sleep)

    await listing._sleep_until_next_announcement(45, NSEC_A)

    # 45s interval: a full 30s tick, then the 15s remainder (never overshoots).
    assert sleeps == [listing.IDENTITY_POLL_SECONDS, 15]
