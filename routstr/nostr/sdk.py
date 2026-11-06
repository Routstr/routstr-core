"""Small adapter around the maintained ``nostr-sdk`` package."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import Any, cast

from nostr_sdk import (
    AckPolicy,
    Client,
    ClientNotification,
    Event,
    EventBuilder,
    Filter,
    Keys,
    Kind,
    PublicKey,
    RelayMessageEnum,
    RelayUrl,
    ReqExitPolicy,
    ReqTarget,
    SendEventTarget,
    Tag,
)


def parse_keypair(secret_key: str) -> tuple[str, str]:
    keys = Keys.parse(secret_key)
    return keys.secret_key().to_hex(), keys.public_key().to_hex()


def generate_keypair() -> tuple[str, str]:
    """Generate a fresh Nostr identity as ``(nsec, npub)`` (both bech32).

    Used to self-provision a node identity on first boot so a fresh deployment
    can announce itself without an operator pasting an nsec. The caller owns
    persisting the secret; this function never logs or stores it.
    """
    keys = Keys.generate()
    return keys.secret_key().to_bech32(), keys.public_key().to_bech32()


def create_signed_event(
    secret_key_hex: str,
    *,
    kind: int,
    content: str,
    tags: list[list[str]],
) -> dict[str, Any]:
    keys = Keys.parse(secret_key_hex)
    event = (
        EventBuilder(Kind(kind), content)
        .tags([Tag.parse(tag) for tag in tags])
        .finalize(keys)
    )
    return cast(dict[str, Any], json.loads(event.as_json()))


async def fetch_events(
    relay_url: str,
    *,
    kind: int,
    author: str,
    limit: int,
    timeout: int,
) -> list[dict[str, Any]]:
    relay = RelayUrl.parse(relay_url)
    client = Client()
    await client.add_relay(relay)
    try:
        await client.connect()
        event_filter = (
            Filter().kinds([Kind(kind)]).authors([PublicKey.parse(author)]).limit(limit)
        )
        events = await client.fetch_events(
            ReqTarget.single(relay, [event_filter]),
            timeout=timedelta(seconds=timeout),
            policy=ReqExitPolicy.WAIT_DURATION_AFTER_EOSE(timedelta(seconds=2.5)),
            max_events=limit,
        )
        return [cast(dict[str, Any], json.loads(event.as_json())) for event in events]
    finally:
        await client.shutdown()


class QueryStatus(str, Enum):
    COMPLETE = "complete"
    TRUNCATED = "truncated"
    FAILED = "failed"


@dataclass(frozen=True)
class EventQueryResult:
    events: list[dict[str, Any]]
    status: QueryStatus


async def fetch_events_with_status(
    relay_url: str,
    *,
    kind: int,
    author: str,
    limit: int,
    timeout: float,
) -> EventQueryResult:
    """Read a bounded page, requiring EOSE rather than buffered SDK results.

    ``Client.fetch_events`` drops individual relay-stream errors. Notifications
    let us distinguish a successful empty page from a failed read. A full page
    is conservatively truncated, even if the relay subsequently sends EOSE.
    """
    if limit < 1:
        raise ValueError("Event query limit must be positive")
    events: dict[str, dict[str, Any]] = {}
    client = Client()
    try:
        async with asyncio.timeout(timeout):
            relay = RelayUrl.parse(relay_url)
            await client.add_relay(relay)
            # Register before sending REQ: even a very fast EOSE must be seen.
            notifications = client.notifications()
            await client.connect()
            subscription_id = uuid.uuid4().hex
            event_filter = (
                Filter()
                .kinds([Kind(kind)])
                .authors([PublicKey.parse(author)])
                .limit(limit)
            )
            output = await client.subscribe(
                ReqTarget.single(relay, [event_filter]), id=subscription_id
            )
            if output.failed or relay not in output.success:
                return EventQueryResult([], QueryStatus.FAILED)
            while notification := await notifications.next():
                if not isinstance(notification, ClientNotification.MESSAGE):
                    continue
                if notification.relay_url != relay:
                    continue
                message = notification.message.as_enum()
                if isinstance(message, RelayMessageEnum.EVENT_MSG):
                    if message.subscription_id != subscription_id:
                        continue
                    event = message.event
                    # MESSAGE notifications include raw events, not just SDK-
                    # accepted NEW_EVENTs. Verify and constrain them ourselves.
                    if (
                        not event.verify()
                        or event.kind().as_u16() != kind
                        or event.author().to_hex() != author
                    ):
                        continue
                    events[event.id().to_hex()] = cast(
                        dict[str, Any], json.loads(event.as_json())
                    )
                    if len(events) >= limit:
                        return EventQueryResult(
                            list(events.values()), QueryStatus.TRUNCATED
                        )
                elif isinstance(message, RelayMessageEnum.END_OF_STORED_EVENTS):
                    if message.subscription_id == subscription_id:
                        return EventQueryResult(
                            list(events.values()), QueryStatus.COMPLETE
                        )
                elif isinstance(message, RelayMessageEnum.CLOSED):
                    if message.subscription_id == subscription_id:
                        return EventQueryResult(
                            list(events.values()), QueryStatus.FAILED
                        )
    except Exception:
        # Keep buffered events for callers doing best-effort discovery, but
        # never let them establish complete identity history.
        return EventQueryResult(list(events.values()), QueryStatus.FAILED)
    finally:
        # Shutdown also closes the subscription and reconnecting relay task.
        await client.shutdown()
    return EventQueryResult(list(events.values()), QueryStatus.FAILED)


async def send_event(relay_url: str, event: dict[str, Any], *, timeout: int) -> None:
    relay = RelayUrl.parse(relay_url)
    client = Client()
    await client.add_relay(relay)
    try:
        await client.connect()
        output = await client.send_event(
            Event.from_json(json.dumps(event)),
            target=SendEventTarget.to([relay]),
            ack_policy=AckPolicy.all(),
            ok_timeout=timedelta(seconds=timeout),
        )
        if output.failed or relay not in output.success:
            reasons = ", ".join(
                f"{url}: {reason}" for url, reason in output.failed.items()
            )
            raise RuntimeError(
                f"Relay did not accept event: {reasons or 'no OK from relay'}"
            )
    finally:
        await client.shutdown()
