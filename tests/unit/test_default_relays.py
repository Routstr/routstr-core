"""Every module that falls back to a default relay list must share one list.

The regression these cover: three modules each carried their own copy of the
default relays, so retiring an unreachable relay meant editing three places and
missing one silently changed behaviour per code path.
"""

from __future__ import annotations

from routstr.nostr import analytics, discovery, listing


def test_default_relays_are_shared_not_duplicated() -> None:
    assert analytics.DEFAULT_RELAYS is listing.DEFAULT_RELAY_URLS
    assert discovery.DEFAULT_RELAY_URLS is listing.DEFAULT_RELAY_URLS


def test_discovery_falls_back_to_the_shared_default_list(monkeypatch) -> None:
    monkeypatch.setattr(discovery.settings, "relays", [])
    assert discovery._get_discovery_relays() == list(listing.DEFAULT_RELAY_URLS)


def test_analytics_falls_back_to_the_shared_default_list(monkeypatch) -> None:
    monkeypatch.setattr(analytics.settings, "relays", [])
    assert analytics._resolve_relays() == list(listing.DEFAULT_RELAY_URLS)


def test_default_relays_are_all_wss() -> None:
    assert listing.DEFAULT_RELAY_URLS
    for url in listing.DEFAULT_RELAY_URLS:
        assert url.startswith("wss://"), url


def test_relay_that_cannot_complete_a_read_is_not_a_default() -> None:
    """relay.nostr.band accepted REQ but never sent EOSE, so every bounded read
    timed out. A failed read is ambiguous identity history to
    resolve_provider_id_strict, which paused stats publication entirely."""
    assert "wss://relay.nostr.band" not in listing.DEFAULT_RELAY_URLS
