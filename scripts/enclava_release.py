"""Sign or publish a public Enclava image release record (no application startup)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from nostr_sdk import (
    AckPolicy,
    Client,
    Event,
    EventBuilder,
    Keys,
    Kind,
    RelayUrl,
    SendEventTarget,
)

ARTIFACT = Path("enclava-release-event.json")


def sign_release(image: str, digest: str, pubkey: str, secret: str) -> Event:
    if not re.fullmatch(
        r"ghcr\.io/[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+",
        image,
    ):
        raise ValueError("expected a lowercase, untagged GHCR image name")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("expected a sha256 image digest")
    if not re.fullmatch(r"[0-9a-f]{64}", pubkey):
        raise ValueError("configure a pinned 32-byte hex release pubkey")
    try:
        keys = Keys.parse(secret)
    except Exception:
        raise ValueError("invalid release signing key") from None
    if keys.public_key().to_hex() != pubkey:
        raise ValueError("signing key does not match the pinned release pubkey")
    content = json.dumps(
        {
            "schema": "routstr.enclava-image-release.v1",
            "scheme": "enclava-tee",
            "image": f"{image}@{digest}",
            "image_digest": digest,
        },
        separators=(",", ":"),
    )
    return EventBuilder(Kind(8428), content).finalize(keys)


async def publish(event: Event, pubkey: str, url: str) -> None:
    if (
        not event.verify()
        or event.kind().as_u16() != 8428
        or event.author().to_hex() != pubkey
    ):
        raise ValueError("not a valid release event from the configured signer")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "wss"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("configure a credential-free wss relay URL")
    relay = RelayUrl.parse(url)
    client = Client()
    try:
        async with asyncio.timeout(45):
            await client.add_relay(relay)
            await client.connect()
            result = await client.send_event(
                event,
                target=SendEventTarget.to([relay]),
                ack_policy=AckPolicy.all(),
                ok_timeout=timedelta(seconds=20),
            )
            if result.failed or relay not in result.success:
                raise RuntimeError("release relay did not acknowledge the event")
    finally:
        await client.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["sign", "publish"])
    command = parser.parse_args().command
    pubkey = os.environ["ENCLAVA_RELEASE_PUBKEY"]
    if command == "sign":
        event = sign_release(
            os.environ["IMAGE_NAME"],
            os.environ["IMAGE_DIGEST"],
            pubkey,
            os.environ["ENCLAVA_RELEASE_NSEC"],
        )
        ARTIFACT.write_text(event.as_json() + "\n", encoding="utf-8")
    else:
        event = Event.from_json(ARTIFACT.read_text(encoding="utf-8"))
        asyncio.run(publish(event, pubkey, os.environ["ENCLAVA_RELEASE_RELAY"]))
        print(f"Acknowledged 8428: {event.id().to_hex()}")
        print(f"Deploy image: {json.loads(event.content())['image']}")


if __name__ == "__main__":
    main()
