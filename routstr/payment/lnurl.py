from __future__ import annotations

import asyncio
import math
from typing import TypedDict

import httpx
from cashu.core.base import MeltQuoteState
from cashu.wallet.wallet import Proof, Wallet

# The Cashu library issues POST /v1/melt/bolt11 with timeout=None, so a hung or
# very slow mint can block a melt (and any caller, e.g. the payout loop)
# indefinitely. Bound both the melt and the follow-up quote reconciliation.
MELT_TIMEOUT_SECONDS = 60
MELT_RECONCILE_TIMEOUT_SECONDS = 10

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


class MeltOutcomeUncertainError(LNURLError):
    """A timed-out melt whose final state could not be established safely."""

    def __init__(self, quote_id: str, reason: str) -> None:
        self.quote_id = quote_id
        super().__init__(f"Melt outcome uncertain for quote {quote_id}: {reason}")


async def _reconcile_timed_out_melt(
    wallet: Wallet, proofs: list[Proof], quote_id: str
) -> None:
    """Resolve a timed-out melt without making reserved proofs spendable early."""
    try:
        quote = await asyncio.wait_for(
            wallet.get_melt_quote(quote_id),
            timeout=MELT_RECONCILE_TIMEOUT_SECONDS,
        )
    except Exception as error:
        raise MeltOutcomeUncertainError(
            quote_id, f"quote reconciliation failed: {type(error).__name__}: {error}"
        ) from error

    if quote is None:
        raise MeltOutcomeUncertainError(quote_id, "mint returned no quote")

    if quote.state == MeltQuoteState.paid:
        try:
            # get_melt_quote updates the wallet database; explicitly invalidate
            # the known-spent inputs so in-memory state cannot expose them.
            await wallet.invalidate(proofs)
            await wallet.load_proofs(reload=True)
        except Exception as error:
            raise MeltOutcomeUncertainError(
                quote_id,
                f"local reconciliation failed: {type(error).__name__}: {error}",
            ) from error
        return

    if quote.state == MeltQuoteState.unpaid:
        try:
            await wallet.set_reserved_for_melt(
                proofs, reserved=False, quote_id=None
            )
            await wallet.load_proofs(reload=True)
        except Exception as error:
            raise MeltOutcomeUncertainError(
                quote_id,
                f"local reconciliation failed: {type(error).__name__}: {error}",
            ) from error
        raise LNURLError(f"Melt timed out and quote {quote_id} is unpaid")

    raise MeltOutcomeUncertainError(quote_id, f"mint reports {quote.state.value}")


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
    wallet: Wallet,
    proofs: list[Proof],
    lnurl: str,
    unit: str,
    amount: int | None = None,
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
    if amount and total_balance < amount:
        raise ValueError("Amount to send is higher than available proofs.")
    else:
        assert isinstance(amount, int)
        total_balance = amount
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

    estimated_fees_sat = int(max(math.ceil((amount_msat / 1000) * 0.01), 2)) + 1
    estimated_fees_msat = estimated_fees_sat * 1000
    final_amount = amount_msat - estimated_fees_msat

    bolt11_invoice, _ = await get_lnurl_invoice(
        lnurl_data["callback_url"], final_amount
    )

    melt_quote_resp = await wallet.melt_quote(invoice=bolt11_invoice)

    if amount:
        proofs, _ = await wallet.select_to_send(proofs, amount, set_reserved=True)

    try:
        _ = await asyncio.wait_for(
            wallet.melt(
                proofs=proofs,
                invoice=bolt11_invoice,
                fee_reserve_sat=melt_quote_resp.fee_reserve,
                quote_id=melt_quote_resp.quote,
            ),
            timeout=MELT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        await _reconcile_timed_out_melt(
            wallet, proofs, melt_quote_resp.quote
        )
    return final_amount
