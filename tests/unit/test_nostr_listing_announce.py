"""Tests for the kind 38421 provider announcement loop.

The regression these cover: the node runner configures the NSEC through the
admin UI (not the ``.env`` file), which only mutates the live ``settings``
singleton. The announcement task must therefore (a) already be running, and
(b) pick the new identity up on its own — without a process restart.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
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

    async def fake_query(*args: Any, **kwargs: Any) -> listing.EventQueryResult:
        return listing.EventQueryResult([], listing.QueryStatus.COMPLETE)

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


@pytest.mark.asyncio
@pytest.mark.parametrize("readable_relay_matches", [False, True])
async def test_relay_that_refuses_reads_still_receives_the_listing(
    monkeypatch: Any, readable_relay_matches: bool
) -> None:
    _quiet_settings(monkeypatch, nsec=NSEC_A)
    monkeypatch.setattr(listing.settings, "relays", ["wss://write-only", "wss://open"])
    published: list[str] = []

    async def fake_query(relay_url: str, *args: Any) -> listing.EventQueryResult:
        if relay_url == "wss://write-only":
            return listing.EventQueryResult([], listing.QueryStatus.FAILED)
        events = [{"id": "existing"}] if readable_relay_matches else []
        return listing.EventQueryResult(events, listing.QueryStatus.COMPLETE)

    async def fake_publish(
        relay_url: str, event: dict[str, Any], timeout: int = 30
    ) -> bool:
        published.append(relay_url)
        return True

    async def stop(*args: Any) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(listing, "query_listing_events", fake_query)
    monkeypatch.setattr(listing, "publish_to_relay", fake_publish)
    monkeypatch.setattr(listing, "events_semantically_equal", lambda *_: True)
    monkeypatch.setattr(listing, "_sleep_until_next_announcement", stop)

    await listing.announce_provider()

    expected = ["wss://write-only"]
    if not readable_relay_matches:
        expected.append("wss://open")
    assert published == expected


@pytest.mark.asyncio
async def test_enclava_deploy_handoff_and_rotation(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A delayed mounted config rotates/removes the claim without admin edits."""
    _quiet_settings(monkeypatch, nsec=NSEC_A)
    monkeypatch.setattr(listing.settings, "http_url", "https://node.example.com")
    monkeypatch.setenv("ROUTSTR_ENCLAVA_BUILD_ID", "1234-1")
    mount = tmp_path / "ROUTSTR_ENCLAVA_LISTING"
    monkeypatch.setattr(listing, "ENCLAVA_LISTING_FILE", mount)
    published = _capture_publishes(monkeypatch)
    monkeypatch.setattr(listing.settings, "relays", ["wss://relay.example"])
    origin = "https://custom.example.com"
    phases = [
        ("1234-1", {origin: "a" * 64}),
        ("1234-1", {origin: "b" * 64}),
        ("1234-1", {}),
    ]
    # Persistent config from a previous build must not be used on first boot.
    mount.write_text(json.dumps({"build_id": "older-1", "origins": {origin: "c" * 64}}))

    async def query(*args: Any) -> listing.EventQueryResult:
        return listing.EventQueryResult(published[-1:], listing.QueryStatus.COMPLETE)

    async def change_config(seconds: float) -> None:
        if not phases:
            raise asyncio.CancelledError()
        build, origins = phases.pop(0)
        mount.write_text(json.dumps({"build_id": build, "origins": origins}))

    monkeypatch.setattr(listing, "query_listing_events", query)
    monkeypatch.setattr(listing.asyncio, "sleep", change_config)
    await listing.announce_provider()
    tags = [event["tags"] for event in published]
    assert [[t for t in ts if t[0] == "attestation"] for ts in tags] == [
        [],
        [["attestation", "enclava-tee", origin, "a" * 64]],
        [["attestation", "enclava-tee", origin, "b" * 64]],
        [],
    ]
    assert ["u", origin] in tags[1] and ["u", origin] not in tags[-1]
    assert all(["u", "https://node.example.com"] in ts for ts in tags)


@pytest.mark.asyncio
async def test_listing_reads_two_origins_without_an_http_url(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _quiet_settings(monkeypatch, nsec=NSEC_A)
    monkeypatch.setattr(listing.settings, "http_url", "")
    monkeypatch.setenv("ROUTSTR_ENCLAVA_BUILD_ID", "1234-1")
    mount = tmp_path / "ROUTSTR_ENCLAVA_LISTING"
    monkeypatch.setattr(listing, "ENCLAVA_LISTING_FILE", mount)
    a, b = "https://one.example", "https://two.example:8443"
    origins = {a: "0" * 64, b: "1" * 64}
    mount.write_text(json.dumps({"build_id": "1234-1", "origins": origins}))
    published = _capture_publishes(monkeypatch)

    async def stop(seconds: float) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(listing.asyncio, "sleep", stop)
    await listing.announce_provider()
    event = published[0]
    for origin, eid in origins.items():
        assert ["u", origin] in event["tags"]
        assert ["attestation", "enclava-tee", origin, eid] in event["tags"]
    changed = {**event, "tags": [list(tag) for tag in event["tags"]]}
    claim = next(tag for tag in changed["tags"] if tag[0] == "attestation")
    claim[2] = b
    assert not listing.events_semantically_equal(event, changed)
    assert listing.events_semantically_equal(
        event, {**event, "tags": event["tags"][::-1]}
    )


@pytest.mark.parametrize(
    "origin, event_id",
    [
        ("http://public.example", "a" * 64),
        ("https://localhost", "a" * 64),
        ("https://user@public.example", "a" * 64),
        ("https://public.example/v1", "a" * 64),
        ("https://public.example:70000", "a" * 64),
        ("https://public.example", "not-an-id"),
    ],
)
def test_invalid_config_never_advertises_an_attestation(
    monkeypatch: Any, tmp_path: Path, origin: str, event_id: str
) -> None:
    mount = tmp_path / "ROUTSTR_ENCLAVA_LISTING"
    monkeypatch.setattr(listing, "ENCLAVA_LISTING_FILE", mount)
    monkeypatch.setenv("ROUTSTR_ENCLAVA_BUILD_ID", "1234-1")
    mount.write_text(json.dumps({"build_id": "1234-1", "origins": {origin: event_id}}))
    assert listing._read_enclava_origins() == {}


def test_repeated_invalid_config_warns_once(monkeypatch: Any, tmp_path: Path) -> None:
    """A persistently bad file must not warn on every 30 s poll tick."""
    mount = tmp_path / "ROUTSTR_ENCLAVA_LISTING"
    monkeypatch.setattr(listing, "ENCLAVA_LISTING_FILE", mount)
    monkeypatch.setenv("ROUTSTR_ENCLAVA_BUILD_ID", "1234-1")
    mount.write_text("{bad")
    warnings: list[str] = []
    monkeypatch.setattr(listing.logger, "warning", lambda msg, *a, **k: warnings.append(msg))
    monkeypatch.setattr(listing.logger, "debug", lambda *a, **k: None)
    for _ in range(5):
        assert listing._read_enclava_origins() == {}
    assert warnings == ["Ignoring unreadable or invalid Enclava listing config"]


@pytest.mark.asyncio
async def test_trailing_slash_endpoint_is_not_published_twice(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """HTTP_URL with a trailing slash must not duplicate the attested origin."""
    _quiet_settings(monkeypatch, nsec=NSEC_A)
    monkeypatch.setattr(listing.settings, "http_url", "https://node.example.com/")
    monkeypatch.setattr(listing.settings, "relays", ["wss://relay.example"])
    monkeypatch.setenv("ROUTSTR_ENCLAVA_BUILD_ID", "1234-1")
    mount = tmp_path / "ROUTSTR_ENCLAVA_LISTING"
    monkeypatch.setattr(listing, "ENCLAVA_LISTING_FILE", mount)
    origin = "https://node.example.com"
    mount.write_text(json.dumps({"build_id": "1234-1", "origins": {origin: "a" * 64}}))
    published = _capture_publishes(monkeypatch)

    async def cancel(seconds: float) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(listing.asyncio, "sleep", cancel)
    await listing.announce_provider()
    tags = published[0]["tags"]
    assert [t[1] for t in tags if t[0] == "u"] == [origin]
    assert ["attestation", "enclava-tee", origin, "a" * 64] in tags
