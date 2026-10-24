"""Nano account checksums, raw quote math and block_info parsing."""

import hashlib

import httpx
import pytest

from routstr.nano import (
    RAW_TAG_MODULUS,
    NanoRpcClient,
    NanoRpcError,
    configured_raw_per_sat,
    decode_account,
    is_block_hash,
    is_valid_account,
    quote_raw,
    same_account,
    tagged_quote_raw,
)

GENESIS = "nano_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3"
GENESIS_KEY = "E89208DD038FBB269987689621D52292AE9C35941A7484756ECCED92A65093BA"
BLOCK_HASH = "A" * 64


def encode_account(public_key: bytes) -> str:
    alphabet = "13456789abcdefghijkmnopqrstuwxyz"
    key = int.from_bytes(public_key, "big")
    check = int.from_bytes(
        hashlib.blake2b(public_key, digest_size=5).digest()[::-1], "big"
    )
    body = "".join(alphabet[(key >> (5 * i)) & 31] for i in range(51, -1, -1))
    tail = "".join(alphabet[(check >> (5 * i)) & 31] for i in range(7, -1, -1))
    return "nano_" + body + tail


def test_decode_genesis_account() -> None:
    assert decode_account(GENESIS).hex().upper() == GENESIS_KEY


def test_xrb_prefix_is_same_account() -> None:
    legacy = "xrb_" + GENESIS[5:]
    assert is_valid_account(legacy)
    assert same_account(legacy, GENESIS)


def test_encode_round_trip() -> None:
    key = bytes(range(32))
    assert decode_account(encode_account(key)) == key


@pytest.mark.parametrize(
    "address",
    [
        GENESIS[:-1] + ("1" if GENESIS[-1] != "1" else "3"),
        GENESIS.replace("nano_", "ban_"),
        GENESIS[:-2],
        GENESIS.upper(),
        "nano_4" + GENESIS[6:],
        "",
    ],
)
def test_invalid_accounts_rejected(address: str) -> None:
    assert not is_valid_account(address)
    with pytest.raises(ValueError):
        decode_account(address)


def test_different_accounts_not_same() -> None:
    assert not same_account(GENESIS, encode_account(bytes(32)))
    assert not same_account(GENESIS, "not-an-account")


def test_quote_raw_is_exact_integer() -> None:
    raw_per_sat = 10**27 + 7
    assert quote_raw(1000, raw_per_sat) == raw_per_sat
    assert quote_raw(1_000_000_000, raw_per_sat) == 1_000_000 * raw_per_sat
    # 1 msat of a 1001-raw sat rounds up to 2 raw.
    assert quote_raw(1, 1001) == 2
    assert isinstance(quote_raw(1000, raw_per_sat), int)


def test_quote_raw_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        quote_raw(0, 10**27)
    with pytest.raises(ValueError):
        quote_raw(1000, 0)


def test_tagged_quote_covers_amount_and_carries_tag() -> None:
    raw_per_sat = 10**27 + 123_456_789
    quote = tagged_quote_raw(5_000_000, raw_per_sat, 42)
    assert quote >= quote_raw(5_000_000, raw_per_sat)
    assert quote % RAW_TAG_MODULUS == 42
    assert quote - quote_raw(5_000_000, raw_per_sat) < RAW_TAG_MODULUS * 2
    with pytest.raises(ValueError):
        tagged_quote_raw(1000, raw_per_sat, 0)
    with pytest.raises(ValueError):
        tagged_quote_raw(1000, raw_per_sat, RAW_TAG_MODULUS)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", None),
        ("abc", None),
        ("1.5e27", None),
        ("-5", None),
        ("999", None),
        ("1000000000000000000000000000", 10**27),
    ],
)
def test_configured_raw_per_sat(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: int | None
) -> None:
    from routstr.core.settings import settings

    monkeypatch.setattr(settings, "nano_raw_per_sat", value)
    assert configured_raw_per_sat() == expected


def test_block_hash_format() -> None:
    assert is_block_hash(BLOCK_HASH)
    assert is_block_hash("ab" * 32)
    assert not is_block_hash("g" * 64)
    assert not is_block_hash("A" * 63)


def _rpc(handler: httpx.MockTransport) -> NanoRpcClient:
    return NanoRpcClient("http://node.test", httpx.AsyncClient(transport=handler))


@pytest.mark.asyncio
async def test_block_info_parses_send_block() -> None:
    seen: list[bytes] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return httpx.Response(
            200,
            json={
                "amount": str(10**30),
                "confirmed": "true",
                "subtype": "send",
                "local_timestamp": "1700000000",
                "contents": {"type": "state", "link_as_account": GENESIS},
            },
        )

    block = await _rpc(httpx.MockTransport(handle)).block_info(BLOCK_HASH)
    assert b'"action":"block_info"' in seen[0].replace(b" ", b"")
    assert block.confirmed and block.subtype == "send"
    assert block.destination == GENESIS
    assert block.amount_raw == 10**30
    assert block.local_timestamp == 1_700_000_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"error": "Block not found"}),
        httpx.Response(500, text="boom"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"amount": "1.5", "contents": {}}),
        httpx.Response(200, json={"amount": "1"}),
    ],
)
async def test_block_info_errors_raise(response: httpx.Response) -> None:
    rpc = _rpc(httpx.MockTransport(lambda request: response))
    with pytest.raises(NanoRpcError):
        await rpc.block_info(BLOCK_HASH)
