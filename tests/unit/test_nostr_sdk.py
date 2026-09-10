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
