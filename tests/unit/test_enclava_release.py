import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nostr_sdk import Event, Keys, RelayUrl

from scripts import enclava_release as release

IMAGE = "ghcr.io/sh1ftred/core"
DIGEST = "sha256:" + "ab" * 32


def test_release_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    keys = Keys.generate()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMAGE_NAME", IMAGE)
    monkeypatch.setenv("IMAGE_DIGEST", DIGEST)
    monkeypatch.setenv("ENCLAVA_RELEASE_PUBKEY", keys.public_key().to_hex())
    monkeypatch.setenv("ENCLAVA_RELEASE_NSEC", keys.secret_key().to_bech32())
    monkeypatch.setattr(sys, "argv", ["publisher", "sign"])
    release.main()
    raw = release.ARTIFACT.read_text()
    event = Event.from_json(raw)
    assert event.verify() and event.kind().as_u16() == 8428
    assert event.author().to_hex() == keys.public_key().to_hex()
    assert json.loads(raw)["tags"] == []
    assert keys.secret_key().to_bech32() not in raw
    assert json.loads(event.content()) == {
        "schema": "routstr.enclava-image-release.v1",
        "scheme": "enclava-tee",
        "image": f"{IMAGE}@{DIGEST}",
        "image_digest": DIGEST,
    }


@pytest.mark.parametrize("case", ["digest", "tag", "pin", "secret"])
def test_refuses_invalid_inputs(case: str) -> None:
    keys = Keys.generate()
    secret = "bad-secret" if case == "secret" else keys.secret_key().to_bech32()
    with pytest.raises(ValueError) as exc:
        release.sign_release(
            IMAGE + (":latest" if case == "tag" else ""),
            "latest" if case == "digest" else DIGEST,
            Keys.generate().public_key().to_hex()
            if case == "pin"
            else keys.public_key().to_hex(),
            secret,
        )
    assert secret not in str(exc.value)


@pytest.mark.parametrize("accepted", [True, False])
async def test_requires_relay_ack(
    accepted: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = Keys.generate()
    event = release.sign_release(
        IMAGE, DIGEST, keys.public_key().to_hex(), keys.secret_key().to_hex()
    )
    url = "wss://relay.example"
    client = AsyncMock()
    client.send_event.return_value = SimpleNamespace(
        success=[RelayUrl.parse(url)] if accepted else [], failed={}
    )
    monkeypatch.setattr(release, "Client", lambda: client)
    if accepted:
        await release.publish(event, keys.public_key().to_hex(), url)
    else:
        with pytest.raises(RuntimeError, match="acknowledge"):
            await release.publish(event, keys.public_key().to_hex(), url)
    client.send_event.assert_awaited_once()
    client.shutdown.assert_awaited_once()


async def test_republish_rejects_untrusted_signer_and_tampering_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = Keys.generate()
    pubkey = keys.public_key().to_hex()
    event = release.sign_release(IMAGE, DIGEST, pubkey, keys.secret_key().to_hex())
    monkeypatch.setattr(
        release, "Client", lambda: pytest.fail("unexpected relay access")
    )
    url = "wss://relay.example"
    with pytest.raises(ValueError, match="configured signer"):
        await release.publish(event, Keys.generate().public_key().to_hex(), url)
    raw = json.loads(event.as_json())
    raw["content"] += " "
    with pytest.raises(ValueError, match="configured signer"):
        await release.publish(Event.from_json(json.dumps(raw)), pubkey, url)
