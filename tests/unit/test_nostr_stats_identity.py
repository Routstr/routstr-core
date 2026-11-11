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
@pytest.mark.parametrize("scenario", ["outage", "empty", "invalid", "raised"])
async def test_no_visible_listing_falls_back_to_pubkey_prefix(
    monkeypatch: Any, scenario: str
) -> None:
    first = listing.create_listing_event(KEY, "one", ["https://node.example"])
    forged = {**first, "sig": "00" * 64}

    async def query(*args: object) -> listing.EventQueryResult:
        if scenario == "raised":
            raise OSError("relay unreachable")
        return {
            "outage": listing.EventQueryResult([], listing.QueryStatus.FAILED),
            "empty": listing.EventQueryResult([], listing.QueryStatus.COMPLETE),
            "invalid": listing.EventQueryResult([forged], listing.QueryStatus.COMPLETE),
        }[scenario]

    monkeypatch.setattr(listing.settings, "provider_id", "")
    monkeypatch.setattr(listing, "query_listing_events", query)
    assert (
        await listing.resolve_provider_id_strict(PUBKEY, ["wss://relay.example"])
        == PUBKEY[:12]
    )


@pytest.mark.asyncio
async def test_no_relays_falls_back_to_pubkey_prefix(monkeypatch: Any) -> None:
    monkeypatch.setattr(listing.settings, "provider_id", "")
    assert await listing.resolve_provider_id_strict(PUBKEY, []) == PUBKEY[:12]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["ambiguous", "truncated"])
async def test_unsafe_identity_history_requires_operator_choice(
    monkeypatch: Any, scenario: str
) -> None:
    first = listing.create_listing_event(KEY, "one", ["https://node.example"])
    second = listing.create_listing_event(KEY, "two", ["https://node.example"])
    result = {
        "ambiguous": listing.EventQueryResult(
            [first, second], listing.QueryStatus.COMPLETE
        ),
        "truncated": listing.EventQueryResult(
            [first] * 10, listing.QueryStatus.TRUNCATED
        ),
    }[scenario]

    async def query(*args: object) -> listing.EventQueryResult:
        return result

    monkeypatch.setattr(listing.settings, "provider_id", "")
    monkeypatch.setattr(listing, "query_listing_events", query)
    with pytest.raises(ValueError, match="PROVIDER_ID"):
        await listing.resolve_provider_id_strict(PUBKEY, ["wss://relay.example"])


@pytest.mark.asyncio
async def test_one_signed_listing_coordinate_is_reused(monkeypatch: Any) -> None:
    event = listing.create_listing_event(KEY, "one", ["https://node.example"])

    async def query(*args: object) -> listing.EventQueryResult:
        return listing.EventQueryResult([event], listing.QueryStatus.COMPLETE)

    monkeypatch.setattr(listing.settings, "provider_id", "")
    monkeypatch.setattr(listing, "query_listing_events", query)
    assert (
        await listing.resolve_provider_id_strict(PUBKEY, ["wss://relay.example"])
        == "one"
    )


@pytest.mark.parametrize("provider_id", ["node\x00", "node\ud800"])
async def test_provider_identity_entrypoints_reject_control_and_surrogate(
    monkeypatch: Any, provider_id: str
) -> None:
    from routstr.nostr.analytics_v2_delivery import (
        IdentityMismatchError,
        _validate_identity,
    )

    monkeypatch.setattr(listing.settings, "provider_id", provider_id)
    with pytest.raises(ValueError, match="PROVIDER_ID"):
        await listing.resolve_provider_id_strict(PUBKEY, [])
    with pytest.raises(IdentityMismatchError, match="control or surrogate"):
        _validate_identity(PUBKEY, provider_id)


async def test_provider_filter_does_not_hide_truncation(monkeypatch: Any) -> None:
    event = listing.create_listing_event(KEY, "other", ["https://node.example"])

    async def fetch(*args: Any, **kwargs: Any) -> listing.EventQueryResult:
        return listing.EventQueryResult([event], listing.QueryStatus.TRUNCATED)

    monkeypatch.setattr(listing, "fetch_events_with_status", fetch)
    result = await listing.query_listing_events("wss://relay.example", PUBKEY, "one")
    assert result.events == []
    assert result.status == listing.QueryStatus.TRUNCATED


async def test_partial_relay_failure_does_not_block_identity_resolution(
    monkeypatch: Any,
) -> None:
    import asyncio
    import json

    from websockets.server import serve

    event = listing.create_listing_event(KEY, "one", ["https://node.example"])

    async def healthy(ws: Any) -> None:
        async for raw in ws:
            message = json.loads(raw)
            if message[0] == "REQ":
                await ws.send(json.dumps(["EVENT", message[1], event]))
                await ws.send(json.dumps(["EOSE", message[1]]))

    async def failed(ws: Any) -> None:
        async for raw in ws:
            if json.loads(raw)[0] == "REQ":
                await ws.close()
                return

    query = listing.query_listing_events

    async def bounded_query(url: str, pubkey: str) -> listing.EventQueryResult:
        return await query(url, pubkey, timeout=0.3)

    monkeypatch.setattr(listing.settings, "provider_id", "")
    monkeypatch.setattr(listing, "query_listing_events", bounded_query)
    async with (
        serve(healthy, "127.0.0.1", 0) as good,
        serve(failed, "127.0.0.1", 0) as bad,
    ):
        urls = [
            f"ws://127.0.0.1:{next(iter(server.sockets)).getsockname()[1]}"
            for server in (good, bad)
        ]
        assert (
            await asyncio.wait_for(
                listing.resolve_provider_id_strict(PUBKEY, urls), timeout=2
            )
            == "one"
        )


@pytest.mark.parametrize(
    ("status", "count"),
    [(listing.QueryStatus.COMPLETE, 10), (listing.QueryStatus.TRUNCATED, 1)],
)
async def test_strict_resolution_rejects_truncation_before_coordinate_deduplication(
    monkeypatch: Any, status: listing.QueryStatus, count: int
) -> None:
    event = listing.create_listing_event(KEY, "one", ["https://node.example"])

    async def query(*args: Any) -> listing.EventQueryResult:
        return listing.EventQueryResult([event] * count, status)

    monkeypatch.setattr(listing.settings, "provider_id", "")
    monkeypatch.setattr(listing, "query_listing_events", query)
    with pytest.raises(ValueError, match="incomplete"):
        await listing.resolve_provider_id_strict(PUBKEY, ["wss://relay.example"])


@pytest.mark.parametrize("status", list(listing.QueryStatus))
async def test_best_effort_listing_discovery_ignores_failed_partial_pages(
    monkeypatch: Any, status: listing.QueryStatus
) -> None:
    event = listing.create_listing_event(KEY, "one", ["https://node.example"])

    async def query(*args: Any) -> listing.EventQueryResult:
        return listing.EventQueryResult([event], status)

    monkeypatch.setattr(listing.settings, "provider_id", "")
    monkeypatch.setattr(listing, "query_listing_events", query)
    assert await listing._determine_provider_id(PUBKEY, ["wss://relay.example"]) == (
        PUBKEY[:12] if status == listing.QueryStatus.FAILED else "one"
    )
