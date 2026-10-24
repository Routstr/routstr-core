"""Nano (XNO) primitives for top-ups: accounts, raw quotes and block lookups.

Amounts are raw (1 XNO = 10**30 raw) and always Python ints; they leave this
module only as decimal strings because they exceed JSON-safe integers.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

import httpx

from .core.settings import settings

_ALPHABET = "13456789abcdefghijkmnopqrstuwxyz"
_ACCOUNT_RE = re.compile(r"^(nano|xrb)_([13][13456789abcdefghijkmnopqrstuwxyz]{59})$")
_BLOCK_HASH_RE = re.compile(r"^[0-9A-Fa-f]{64}$")

# Quotes carry a per-invoice tag in their lowest raw digits so a send block
# matches exactly one open invoice. The tag is worth far less than one sat.
RAW_TAG_MODULUS = 1_000_000


class NanoRpcError(Exception):
    """The RPC node failed or returned something that is not a block."""


def _decode_base32(chars: str) -> int:
    value = 0
    for char in chars:
        value = (value << 5) | _ALPHABET.index(char)
    return value


def decode_account(address: str) -> bytes:
    """Return the 32-byte public key of a nano_/xrb_ address, checking its checksum."""
    match = _ACCOUNT_RE.match(address)
    if not match:
        raise ValueError("Not a nano_ or xrb_ account")
    body = match.group(2)
    public_key = _decode_base32(body[:52]).to_bytes(32, "big")
    checksum = _decode_base32(body[52:]).to_bytes(5, "big")
    expected = hashlib.blake2b(public_key, digest_size=5).digest()[::-1]
    if checksum != expected:
        raise ValueError("Account checksum mismatch")
    return public_key


def is_valid_account(address: str) -> bool:
    try:
        decode_account(address)
    except ValueError:
        return False
    return True


def same_account(a: str, b: str) -> bool:
    """Compare two accounts by public key, so nano_ and xrb_ forms are equal."""
    try:
        return decode_account(a) == decode_account(b)
    except ValueError:
        return False


def is_block_hash(value: str) -> bool:
    return bool(_BLOCK_HASH_RE.match(value))


def configured_raw_per_sat() -> int | None:
    """The operator's XNO rate in raw per sat, or None when no offer is possible."""
    value = settings.nano_raw_per_sat.strip()
    if not value.isdigit():
        return None
    rate = int(value)
    return rate if rate >= RAW_TAG_MODULUS else None


def quote_raw(amount_msats: int, raw_per_sat: int) -> int:
    """Raw owed for ``amount_msats``, rounded up to the next raw."""
    if amount_msats <= 0 or raw_per_sat <= 0:
        raise ValueError("amount and rate must be positive")
    return -(-amount_msats * raw_per_sat // 1000)


def tagged_quote_raw(amount_msats: int, raw_per_sat: int, tag: int) -> int:
    """Quote rounded up to a multiple of RAW_TAG_MODULUS, plus a tag below it."""
    if not 0 < tag < RAW_TAG_MODULUS:
        raise ValueError("tag out of range")
    base = quote_raw(amount_msats, raw_per_sat)
    base = -(-base // RAW_TAG_MODULUS) * RAW_TAG_MODULUS
    return base + tag


@dataclass(frozen=True)
class NanoBlock:
    hash: str
    confirmed: bool
    subtype: str
    destination: str
    amount_raw: int
    local_timestamp: int


def _parse_block_info(block_hash: str, data: Any) -> NanoBlock:
    if not isinstance(data, dict):
        raise NanoRpcError("block_info returned a non-object")
    if "error" in data:
        raise NanoRpcError(f"block_info error: {data['error']}")
    contents = data.get("contents")
    if not isinstance(contents, dict):
        raise NanoRpcError("block_info returned no json_block contents")
    amount = str(data.get("amount", ""))
    timestamp = str(data.get("local_timestamp", "0"))
    if not amount.isdigit() or not timestamp.isdigit():
        raise NanoRpcError("block_info returned a malformed amount or timestamp")
    return NanoBlock(
        hash=block_hash.upper(),
        confirmed=str(data.get("confirmed", "")).lower() == "true",
        subtype=str(data.get("subtype", "")),
        destination=str(contents.get("link_as_account", "")),
        amount_raw=int(amount),
        local_timestamp=int(timestamp),
    )


class NanoRpcClient:
    """Minimal JSON-RPC client for one Nano node."""

    def __init__(self, url: str, http_client: httpx.AsyncClient | None = None) -> None:
        self.url = url
        self._http_client = http_client

    async def _post(self, payload: dict[str, str]) -> Any:
        try:
            if self._http_client is not None:
                response = await self._http_client.post(self.url, json=payload)
            else:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.post(self.url, json=payload)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise NanoRpcError(f"Nano RPC request failed: {error}") from error

    async def block_info(self, block_hash: str) -> NanoBlock:
        data = await self._post(
            {"action": "block_info", "json_block": "true", "hash": block_hash}
        )
        return _parse_block_info(block_hash, data)


def get_nano_rpc() -> NanoRpcClient:
    return NanoRpcClient(settings.nano_rpc_url)
