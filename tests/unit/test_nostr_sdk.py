from __future__ import annotations

import json
from typing import Any

import pytest
from nostr_sdk import Event, SendEventOutput

from routstr.nostr import sdk
from routstr.nostr.listing import create_listing_event, nsec_to_keypair

PRIVATE_KEY_HEX = "11" * 32
PRIVATE_KEY_NSEC = "nsec1zyg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3zyg3zygs4rm7hz"
PUBLIC_KEY_HEX = "4f355bdcb7cc0af728ef3cceb9615d90684bb5b2ca5f859ab0f0b704075871aa"


def test_nsec_and_hex_parse_to_same_keypair() -> None:
    expected = (PRIVATE_KEY_HEX, PUBLIC_KEY_HEX)

    assert nsec_to_keypair(PRIVATE_KEY_HEX) == expected
    assert nsec_to_keypair(PRIVATE_KEY_NSEC) == expected


def test_listing_event_is_valid_nip01_event() -> None:
    event = create_listing_event(
        PRIVATE_KEY_HEX,
        "provider123",
        ["https://provider.example.com"],
        mint_urls=["https://mint.example.com"],
        version="1.2.3",
        metadata={"name": "Provider"},
    )

    assert event["pubkey"] == PUBLIC_KEY_HEX
    assert event["kind"] == 38421
    assert Event.from_json(json.dumps(event)).verify()


class FakeClient:
    def __init__(
        self,
        event: dict[str, Any] | None = None,
        send_failure: str | None = None,
    ) -> None:
        self.event = event
        self.send_failure = send_failure
        self.relay: Any = None
        self.connected = False
        self.shutdown_called = False
        self.sent_event: Event | None = None

    async def add_relay(self, relay: Any) -> bool:
        self.relay = relay
        return True

    async def connect(self) -> None:
        self.connected = True

    async def fetch_events(self, *args: Any, **kwargs: Any) -> list[Event]:
        assert self.event is not None
        return [Event.from_json(json.dumps(self.event))]

    async def send_event(self, event: Event, **kwargs: Any) -> SendEventOutput:
        self.sent_event = event
        if self.send_failure is not None:
            return SendEventOutput(
                id=event.id(), success=[], failed={self.relay: self.send_failure}
            )
        return SendEventOutput(id=event.id(), success=[self.relay], failed={})

    async def shutdown(self) -> None:
        self.shutdown_called = True


@pytest.mark.asyncio
async def test_fetch_events_uses_sdk_client_and_closes_it(monkeypatch: Any) -> None:
    event = create_listing_event(
        PRIVATE_KEY_HEX,
        "provider123",
        ["https://provider.example.com"],
    )
    client = FakeClient(event)
    monkeypatch.setattr(sdk, "Client", lambda: client)

    fetched = await sdk.fetch_events(
        "wss://relay.example.com",
        kind=38421,
        author=PUBLIC_KEY_HEX,
        limit=10,
        timeout=30,
    )

    assert fetched == [event]
    assert client.connected
    assert client.shutdown_called


@pytest.mark.asyncio
async def test_send_event_uses_sdk_client_and_closes_it(monkeypatch: Any) -> None:
    event = create_listing_event(
        PRIVATE_KEY_HEX,
        "provider123",
        ["https://provider.example.com"],
    )
    client = FakeClient()
    monkeypatch.setattr(sdk, "Client", lambda: client)

    await sdk.send_event("wss://relay.example.com", event, timeout=30)

    assert client.connected
    assert client.sent_event is not None
    assert client.sent_event.verify()
    assert client.shutdown_called


@pytest.mark.asyncio
async def test_send_event_raises_when_relay_rejects(monkeypatch: Any) -> None:
    event = create_listing_event(
        PRIVATE_KEY_HEX,
        "provider123",
        ["https://provider.example.com"],
    )
    client = FakeClient(send_failure="blocked: rate limited")
    monkeypatch.setattr(sdk, "Client", lambda: client)

    with pytest.raises(RuntimeError, match="rate limited"):
        await sdk.send_event("wss://relay.example.com", event, timeout=30)

    assert client.shutdown_called


@pytest.mark.parametrize(
    ("behavior", "expected"),
    [
        ("empty", sdk.QueryStatus.COMPLETE),
        ("event", sdk.QueryStatus.COMPLETE),
        ("disconnect", sdk.QueryStatus.FAILED),
        ("partial-disconnect", sdk.QueryStatus.FAILED),
        ("no-eose", sdk.QueryStatus.FAILED),
        ("wrong-eose", sdk.QueryStatus.FAILED),
        ("closed", sdk.QueryStatus.FAILED),
        ("truncated", sdk.QueryStatus.TRUNCATED),
    ],
)
async def test_query_status_requires_matching_eose_from_real_relay(
    behavior: str, expected: sdk.QueryStatus
) -> None:
    import asyncio
    import time

    from nostr_sdk import EventBuilder, Keys, Kind, Tag, Timestamp
    from websockets.server import serve

    # Separately signed versions sharing d must not hide page truncation.
    events = [
        json.loads(
            EventBuilder(Kind(38421), "")
            .tags([Tag.parse(["d", "one"])])
            .custom_created_at(Timestamp.from_secs(int(time.time()) - offset))
            .finalize(Keys.parse(PRIVATE_KEY_HEX))
            .as_json()
        )
        for offset in range(3)
    ]

    async def relay(ws: Any) -> None:
        async for raw in ws:
            message = json.loads(raw)
            if message[0] != "REQ":
                continue
            subscription = message[1]
            if behavior == "disconnect":
                await ws.close()
                return
            if behavior in ("event", "partial-disconnect", "no-eose"):
                await ws.send(json.dumps(["EVENT", subscription, events[0]]))
            if behavior == "partial-disconnect":
                await ws.close()
                return
            if behavior == "truncated":
                for event in events:
                    await ws.send(json.dumps(["EVENT", subscription, event]))
            if behavior == "closed":
                await ws.send(json.dumps(["CLOSED", subscription, "blocked"]))
            elif behavior == "wrong-eose":
                await ws.send(json.dumps(["EOSE", "unrelated-subscription"]))
            elif behavior != "no-eose":
                await ws.send(json.dumps(["EOSE", subscription]))

    async with serve(relay, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{next(iter(server.sockets)).getsockname()[1]}"
        result = await asyncio.wait_for(
            sdk.fetch_events_with_status(
                url, kind=38421, author=PUBLIC_KEY_HEX, limit=3, timeout=0.3
            ),
            timeout=2,
        )
    assert result.status == expected
    assert len(result.events) == (
        3
        if behavior == "truncated"
        else 1
        if behavior in ("event", "partial-disconnect", "no-eose")
        else 0
    )


async def test_status_query_deduplicates_and_rejects_unrelated_or_forged_events() -> (
    None
):
    from websockets.server import serve

    event = create_listing_event(PRIVATE_KEY_HEX, "one", ["https://node.example"])
    other_author = create_listing_event("22" * 32, "two", ["https://node.example"])
    forged = {**event, "sig": "00" * 64}
    wrong_kind = sdk.create_signed_event(PRIVATE_KEY_HEX, kind=1, content="", tags=[])

    async def relay(ws: Any) -> None:
        async for raw in ws:
            message = json.loads(raw)
            if message[0] == "REQ":
                for candidate in [event, event, other_author, forged, wrong_kind]:
                    await ws.send(json.dumps(["EVENT", message[1], candidate]))
                await ws.send(json.dumps(["EOSE", message[1]]))

    async with serve(relay, "127.0.0.1", 0) as server:
        result = await sdk.fetch_events_with_status(
            f"ws://127.0.0.1:{next(iter(server.sockets)).getsockname()[1]}",
            kind=38421,
            author=PUBLIC_KEY_HEX,
            limit=3,
            timeout=1,
        )
    assert result == sdk.EventQueryResult([event], sdk.QueryStatus.COMPLETE)


async def test_status_query_cancellation_closes_relay_subscription() -> None:
    import asyncio

    from websockets.server import serve

    requested = asyncio.Event()
    closed = asyncio.Event()

    async def relay(ws: Any) -> None:
        try:
            async for raw in ws:
                if json.loads(raw)[0] == "REQ":
                    requested.set()
        finally:
            closed.set()

    async with serve(relay, "127.0.0.1", 0) as server:
        query = asyncio.create_task(
            sdk.fetch_events_with_status(
                f"ws://127.0.0.1:{next(iter(server.sockets)).getsockname()[1]}",
                kind=38421,
                author=PUBLIC_KEY_HEX,
                limit=3,
                timeout=30,
            )
        )
        await asyncio.wait_for(requested.wait(), timeout=2)
        query.cancel()
        with pytest.raises(asyncio.CancelledError):
            await query
        await asyncio.wait_for(closed.wait(), timeout=2)
