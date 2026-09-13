"""Small adapter around the maintained ``nostr-sdk`` package."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, cast

from nostr_sdk import (
    AckPolicy,
    Client,
    Event,
    EventBuilder,
    Filter,
    Keys,
    Kind,
    PublicKey,
    RelayUrl,
    ReqExitPolicy,
    ReqTarget,
    SendEventTarget,
    Tag,
)


def parse_keypair(secret_key: str) -> tuple[str, str]:
    keys = Keys.parse(secret_key)
    return keys.secret_key().to_hex(), keys.public_key().to_hex()


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
