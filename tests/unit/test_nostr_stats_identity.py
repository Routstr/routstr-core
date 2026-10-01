from __future__ import annotations

from typing import Any

import pytest

from routstr.nostr import listing

KEY = "11" * 32
KEYPAIR = listing.nsec_to_keypair(KEY)
assert KEYPAIR is not None
PUBKEY = KEYPAIR[1]


@pytest.mark.asyncio
async def test_explicit_identity_works_without_relay_reads(monkeypatch: Any) -> None:
    monkeypatch.setattr(listing.settings, "provider_id", "my-node")
    assert await listing.resolve_provider_id_strict(PUBKEY, []) == "my-node"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", ["outage", "empty", "ambiguous", "invalid", "truncated"]
)
async def test_unsafe_identity_history_requires_operator_choice(
    monkeypatch: Any, scenario: str
) -> None:
    first = listing.create_listing_event(KEY, "one", ["https://node.example"])
    second = listing.create_listing_event(KEY, "two", ["https://node.example"])
    forged = {**first, "sig": "00" * 64}
    result = {
        "outage": ([], False),
        "empty": ([], True),
        "ambiguous": ([first, second], True),
        "invalid": ([forged], True),
        "truncated": ([first] * 10, True),
    }[scenario]

    async def query(*args: object) -> tuple[list, bool]:
        return result

    monkeypatch.setattr(listing.settings, "provider_id", "")
    monkeypatch.setattr(listing, "query_listing_events", query)
    with pytest.raises(ValueError, match="PROVIDER_ID"):
        await listing.resolve_provider_id_strict(PUBKEY, ["wss://relay.example"])


@pytest.mark.asyncio
async def test_one_signed_listing_coordinate_is_reused(monkeypatch: Any) -> None:
    event = listing.create_listing_event(KEY, "one", ["https://node.example"])

    async def query(*args: object) -> tuple[list, bool]:
        return [event], True

    monkeypatch.setattr(listing.settings, "provider_id", "")
    monkeypatch.setattr(listing, "query_listing_events", query)
    assert (
        await listing.resolve_provider_id_strict(PUBKEY, ["wss://relay.example"])
        == "one"
    )
