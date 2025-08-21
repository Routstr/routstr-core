from __future__ import annotations

import math
from typing import TypedDict

import httpx
from cashu.wallet.wallet import Proof, Wallet

try:
    from bech32 import bech32_decode, convertbits  # type: ignore
except ModuleNotFoundError:  # pragma: no cover – allow runtime miss
    bech32_decode = None  # type: ignore
    convertbits = None  # type: ignore


class LNURLData(TypedDict):
    """LNURL payRequest data."""

    callback_url: str
    min_sendable: int  # millisatoshi
    max_sendable: int  # millisatoshi


class LNURLError(Exception):
    """LNURL related errors."""


def parse_lightning_invoice_amount(invoice: str, currency: str = "sat") -> int:
    """Parse Lightning invoice (BOLT-11) to extract amount in specified currency units.

    Args:
        invoice: BOLT-11 Lightning invoice string
        currency: Target currency unit ("sat" or "msat")

    Returns:
        Amount in the specified currency unit

    Raises:
        LNURLError: If invoice format is invalid or amount cannot be parsed
    """
    invoice = invoice.lower().strip()

    if not invoice.startswith("ln"):
        raise LNURLError("Invalid Lightning invoice format")

    # Find the network part (bc, tb, etc.)
    network_start = 2
    while network_start < len(invoice) and invoice[network_start] not in "0123456789":
        network_start += 1

    if network_start >= len(invoice):
        raise LNURLError("Invalid Lightning invoice format")

    # Parse amount and multiplier
    amount_str = ""
    multiplier = ""
    i = network_start

    # Extract numeric part
    while i < len(invoice) and invoice[i].isdigit():
        amount_str += invoice[i]
        i += 1

    # Extract multiplier if present
    if i < len(invoice) and invoice[i] in "munp":
        multiplier = invoice[i]
        i += 1

    # Check if we have the required "1" separator
    if i >= len(invoice) or invoice[i] != "1":
        raise LNURLError("Invalid Lightning invoice format")

    if not amount_str:
        raise LNURLError("Lightning invoice amount not specified")

    # Convert to base units
    try:
        amount = int(amount_str)
    except ValueError:
        raise LNURLError("Invalid Lightning invoice amount")

    # Apply multiplier to get millisatoshis
    if multiplier == "m":  # milli = 10^-3
        amount_msat = amount * 100_000_000  # amount is in BTC * 10^-3
    elif multiplier == "u":  # micro = 10^-6
        amount_msat = amount * 100_000  # amount is in BTC * 10^-6
    elif multiplier == "n":  # nano = 10^-9
        amount_msat = amount * 100  # amount is in BTC * 10^-9
    elif multiplier == "p":  # pico = 10^-12
        amount_msat = amount // 10  # amount is in BTC * 10^-12
    else:
        # No multiplier means the amount is in BTC
        amount_msat = amount * 100_000_000_000  # Convert BTC to msat

    # Convert to target currency unit
    if currency == "msat":
        return amount_msat
    elif currency == "sat":
        return amount_msat // 1000
    else:
        raise LNURLError(f"Unsupported currency for Lightning: {currency}")


async def decode_lnurl(lnurl: str) -> str:
    """Decode LNURL to get the actual URL.

    Handles:
    - lightning: prefix
    - user@host format
    - bech32 encoded lnurl
    - direct HTTPS URLs

    Args:
        lnurl: LNURL string in any supported format

    Returns:
        The decoded HTTPS URL

    Raises:
        LNURLError: If the LNURL format is invalid
    """
    # Remove lightning: prefix if present
    if lnurl.startswith("lightning:"):
        lnurl = lnurl[10:]

    # Handle user@host format (Lightning Address)
    if "@" in lnurl and len(lnurl.split("@")) == 2:
        user, host = lnurl.split("@")
        return f"https://{host}/.well-known/lnurlp/{user}"

    # Handle bech32 encoded LNURL
    if lnurl.lower().startswith("lnurl"):
        if bech32_decode is None or convertbits is None:
            raise ImportError(
                "bech32 library is required for LNURL bech32 decoding. "
                "Install it with: pip install bech32"
            )

        try:
            hrp, data = bech32_decode(lnurl)
            if data is None:
                raise LNURLError("Invalid bech32 data in LNURL")

            decoded_data = convertbits(data, 5, 8, False)
            if decoded_data is None:
                raise LNURLError("Failed to convert LNURL bits")

            return bytes(decoded_data).decode("utf-8")
        except Exception as e:
            raise LNURLError(f"Failed to decode LNURL: {e}") from e

    # Assume it's a direct URL
    if not lnurl.startswith("https://"):
        raise LNURLError("Direct LNURL must use HTTPS")

    return lnurl


async def get_lnurl_data(lnurl: str) -> LNURLData:
    """Fetch LNURL payRequest data.

    Args:
        lnurl: LNURL string in any supported format

    Returns:
        LNURLData with callback URL and sendable amounts

    Raises:
        LNURLError: If the LNURL data is invalid
        httpx.HTTPError: If the HTTP request fails
    """
    url = await decode_lnurl(lnurl)

    async with httpx.AsyncClient() as client:
        response = await client.get(url, follow_redirects=True, timeout=10)
        response.raise_for_status()

    lnurl_data = response.json()

    # Validate payRequest data
    if lnurl_data.get("tag") != "payRequest":
        raise LNURLError(
            f"Invalid LNURL tag: expected 'payRequest', got '{lnurl_data.get('tag')}'"
        )

    if not isinstance(lnurl_data.get("callback"), str):
        raise LNURLError("Invalid LNURL payRequest: missing callback URL")

    return LNURLData(
        callback_url=lnurl_data["callback"],
        min_sendable=lnurl_data.get("minSendable", 1000),  # Default 1 sat
        max_sendable=lnurl_data.get("maxSendable", 1000000000),  # Default 1000 BTC
    )


async def get_lnurl_invoice(
    callback_url: str, amount_msat: int
) -> tuple[str, dict[str, object]]:
    """Request a Lightning invoice from LNURL callback.

    Args:
        callback_url: The LNURL callback URL
        amount_msat: Amount in millisatoshi

    Returns:
        Tuple of (bolt11_invoice, full_response_data)

    Raises:
        LNURLError: If the response is invalid
        httpx.HTTPError: If the HTTP request fails
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            callback_url,
            params={"amount": amount_msat},
            follow_redirects=True,
            timeout=10,
        )
        response.raise_for_status()

    invoice_data = response.json()

    if "pr" not in invoice_data:
        # Check if there's an error in the response
        if "reason" in invoice_data:
            raise LNURLError(f"LNURL error: {invoice_data['reason']}")
        raise LNURLError(f"Invalid LNURL invoice response: {invoice_data}")

    return invoice_data["pr"], invoice_data


async def raw_send_to_lnurl(
    wallet: Wallet, proofs: list[Proof], lnurl: str, unit: str
) -> int:
    """Send funds to an LNURL address.

    Args:
        wallet: Wallet instance
        lnurl: LNURL string (can be lightning:, user@host, bech32, or direct URL)
        amount: Amount to send in the specified currency unit

    Returns:
        Amount actually paid in the specified currency unit

    Raises:
        WalletError: If amount is outside LNURL limits or insufficient balance
        LNURLError: If LNURL operations fail

    Example:
        # Send 1000 sats to a Lightning Address
        paid = await wallet.send_to_lnurl("user@getalby.com", 1000)
        print(f"Paid {paid} sats")

        # Send USD to Lightning Address
        paid = await wallet.send_to_lnurl("user@getalby.com", 50, unit="usd")
    """
    total_balance = sum(proof.amount for proof in proofs)
    lnurl_data = await get_lnurl_data(lnurl)

    if unit == "sat":
        amount_msat = total_balance * 1000
        min_sendable_sat = lnurl_data["min_sendable"] // 1000
        max_sendable_sat = lnurl_data["max_sendable"] // 1000
    elif unit == "msat":
        amount_msat = (total_balance // 1000) * 1000
        min_sendable_sat = lnurl_data["min_sendable"]
        max_sendable_sat = lnurl_data["max_sendable"]
    else:
        raise ValueError(f"Currency {unit} not supported for LNURL")

    if not (lnurl_data["min_sendable"] <= amount_msat <= lnurl_data["max_sendable"]):
        raise ValueError(
            f"Amount {total_balance} {unit} is outside LNURL limits "
            f"({min_sendable_sat} - {max_sendable_sat} {unit})"
        )

    estimated_fees_sat = int(max(math.ceil((amount_msat / 1000) * 0.01), 2))
    estimated_fees_msat = estimated_fees_sat * 1000
    final_amount = amount_msat - estimated_fees_msat

    print(f"Final amount: {final_amount} {unit}")
    print(f"Estimated fees: {estimated_fees_msat} msat")
    print(f"Amount before fees: {amount_msat} {unit}")
    bolt11_invoice, _ = await get_lnurl_invoice(
        lnurl_data["callback_url"], final_amount
    )
    print(f"Bolt11 invoice: {bolt11_invoice}")

    melt_quote_resp = await wallet.melt_quote(
        invoice=bolt11_invoice, amount_msat=final_amount
    )
    print(melt_quote_resp)
    _ = await wallet.melt(
        proofs=proofs,
        invoice=bolt11_invoice,
        fee_reserve_sat=melt_quote_resp.fee_reserve,
        quote_id=melt_quote_resp.quote,
    )
    return final_amount
