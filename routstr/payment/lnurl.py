from __future__ import annotations

import ipaddress
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

import httpx
from cashu.core.base import MeltQuoteState
from cashu.wallet.wallet import Proof, Wallet

from ..mint import (
    is_mint_rate_limited,
    is_mint_transport_error,
    run_mint_operation,
)

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


class MeltOutcomeAmbiguousError(LNURLError):
    """A melt was dispatched but its final outcome could not be confirmed.

    Callers must NOT treat this as a clean failure: the payment may still
    settle, so debits backing it must be kept until reconciliation confirms
    the true outcome.
    """


_MAX_LNURL_REDIRECTS = 3
_NON_PUBLIC_HOST_SUFFIXES = (".localhost", ".local", ".internal")


def _require_public_https_destination(url: httpx.URL) -> None:
    """Reject anything that is not a public HTTPS endpoint.

    LNURL destinations and their redirect targets are attacker-influenced, so
    every hop has to be re-checked: a single ``https://`` origin says nothing
    about where a 302 points.
    """
    if url.scheme != "https":
        raise LNURLError("LNURL destination must be an HTTPS URL")

    host = (url.host or "").rstrip(".").lower()
    if not host:
        raise LNURLError("LNURL destination has no host")

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host == "localhost" or host.endswith(_NON_PUBLIC_HOST_SUFFIXES):
            raise LNURLError("LNURL destination is not a public host") from None
        return

    if not address.is_global:
        raise LNURLError("LNURL destination is not a public host")


async def _fetch_lnurl_json(
    url: str, params: dict[str, int] | None = None
) -> dict[str, Any]:
    """GET an LNURL endpoint, validating the destination at every redirect.

    Response bodies are never echoed: an LNURL service is untrusted, and its
    payload would otherwise reach operator logs through raised errors.
    """
    try:
        target = httpx.URL(url, params=params) if params else httpx.URL(url)
    except httpx.InvalidURL as e:
        raise LNURLError("LNURL destination is not a usable URL") from e
    _require_public_https_destination(target)

    async with httpx.AsyncClient() as client:
        for _ in range(_MAX_LNURL_REDIRECTS + 1):
            response = await client.get(target, follow_redirects=False, timeout=10)
            if not response.is_redirect:
                break
            target = target.join(response.headers.get("location", ""))
            _require_public_https_destination(target)
        else:
            raise LNURLError("LNURL destination exceeded the redirect limit")
        response.raise_for_status()

    try:
        data = response.json()
    except ValueError as e:
        raise LNURLError("LNURL response was not valid JSON") from e

    if not isinstance(data, dict):
        raise LNURLError("LNURL response was not a JSON object")
    return data


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
    lnurl_data = await _fetch_lnurl_json(url)

    # Validate payRequest data
    if lnurl_data.get("tag") != "payRequest":
        raise LNURLError("Invalid LNURL tag: expected 'payRequest'")

    callback_url = lnurl_data.get("callback")
    if not isinstance(callback_url, str):
        raise LNURLError("Invalid LNURL payRequest: missing callback URL")
    try:
        _require_public_https_destination(httpx.URL(callback_url))
    except httpx.InvalidURL as e:
        raise LNURLError("Invalid LNURL callback URL") from e

    min_sendable = lnurl_data.get("minSendable", 1000)  # Default 1 sat
    max_sendable = lnurl_data.get("maxSendable", 1000000000)  # Default 1000 BTC
    if not isinstance(min_sendable, int) or not isinstance(max_sendable, int):
        raise LNURLError("Invalid LNURL payRequest: non-integer sendable limits")

    return LNURLData(
        callback_url=callback_url,
        min_sendable=min_sendable,
        max_sendable=max_sendable,
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
    invoice_data = await _fetch_lnurl_json(callback_url, params={"amount": amount_msat})

    if not isinstance(invoice_data.get("pr"), str):
        raise LNURLError("LNURL callback returned no invoice")

    return invoice_data["pr"], invoice_data


def _select_melt_proofs(
    wallet: Wallet,
    proofs: list[Proof],
    *,
    quote_amount: int,
    fee_reserve: int,
    gross_budget: int,
) -> tuple[list[Proof] | None, int]:
    """Select proofs that cover the quote and exact NUT-02 input fees.

    Cashu 0.20's ``select_to_send`` may recursively swap when asked to spend a
    wallet's full balance. Melts accept overpayment and return change, so a
    bounded, largest-first selection is both safer and minimizes input fees.
    """
    selected: list[Proof] = []
    selected_amount = 0
    required = quote_amount + fee_reserve
    for proof in sorted(proofs, key=lambda item: item.amount, reverse=True):
        if getattr(proof, "reserved", False) is True:
            continue
        selected.append(proof)
        selected_amount += proof.amount
        input_fees = int(wallet.get_fees_for_proofs(selected))
        required = quote_amount + fee_reserve + input_fees
        if selected_amount >= required:
            if required <= gross_budget:
                return selected, 0
            # Covered but over budget; more proofs only raise input fees.
            break
    return None, max(1, required - min(selected_amount, gross_budget))


async def raw_send_to_lnurl(
    wallet: Wallet,
    proofs: list[Proof],
    lnurl: str,
    unit: str,
    amount: int | None = None,
    *,
    on_melt_quote: Callable[[str], Awaitable[None]] | None = None,
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
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise ValueError("A positive integer amount is required to send to an LNURL.")
    if sum(proof.amount for proof in proofs) < amount:
        raise ValueError("Amount to send is higher than available proofs.")
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

    final_amount = amount_msat

    selected_proofs: list[Proof] | None = None
    # Find the largest amount covered by the budget after reserve and input fees.
    for _ in range(8):
        if final_amount < lnurl_data["min_sendable"]:
            raise LNURLError("Cashu melt fees leave no payable LNURL amount")
        bolt11_invoice, _ = await get_lnurl_invoice(
            lnurl_data["callback_url"], final_amount
        )
        melt_quote_resp = await run_mint_operation(
            lambda: wallet.melt_quote(invoice=bolt11_invoice),
            op_name="lnurl_melt_quote",
            mint_url=str(wallet.url),
            # Quote creation is unsafe to retry without idempotency.
            retry_timeouts=False,
        )

        quoted_amount = int(melt_quote_resp.amount)
        expected_amount = final_amount // 1000 if unit == "sat" else final_amount
        if quoted_amount != expected_amount:
            raise LNURLError(
                f"LNURL invoice amount does not match the requested amount "
                f"(quoted {quoted_amount} {unit}, expected {expected_amount} {unit})"
            )

        selected_proofs, shortfall = _select_melt_proofs(
            wallet,
            proofs,
            quote_amount=quoted_amount,
            fee_reserve=int(melt_quote_resp.fee_reserve),
            gross_budget=amount,
        )
        if selected_proofs is not None:
            break
        final_amount -= shortfall * (1000 if unit == "sat" else 1)
    else:
        raise LNURLError("Cashu melt fees exceed the requested gross amount")

    if on_melt_quote is not None:
        await on_melt_quote(melt_quote_resp.quote)

    assert selected_proofs is not None
    proofs = selected_proofs
    await wallet.set_reserved_for_send(proofs, reserved=True)

    try:
        melt_response = await run_mint_operation(
            lambda: wallet.melt(
                proofs=proofs,
                invoice=bolt11_invoice,
                fee_reserve_sat=melt_quote_resp.fee_reserve,
                quote_id=melt_quote_resp.quote,
            ),
            op_name="lnurl_melt",
            mint_url=str(wallet.url),
            retry_timeouts=False,
        )
    except Exception as error:
        if is_mint_rate_limited(error):
            # Cooldown failures happen before dispatch, and HTTP 429 means the
            # mint rejected the request. Neither outcome may keep proofs
            # reserved as though a Lightning payment could still settle.
            await wallet.set_reserved_for_send(proofs, reserved=False)
            raise
        if not is_mint_transport_error(error):
            raise
        # Cashu clears reservations on transport errors despite an unknown outcome.
        try:
            await wallet.set_reserved_for_melt(
                proofs, reserved=True, quote_id=melt_quote_resp.quote
            )
        except Exception as reservation_error:
            raise MeltOutcomeAmbiguousError(
                "Melt outcome is ambiguous and its proof reservation could not "
                "be restored; proofs must not be retried"
            ) from reservation_error
        melt_response = None
        melt_error: BaseException | None = error
    else:
        melt_error = None

    melt_state = getattr(melt_response, "state", None)
    if melt_state == MeltQuoteState.paid:
        return final_amount
    if melt_state == MeltQuoteState.unpaid:
        await wallet.set_reserved_for_send(proofs, reserved=False)
        raise LNURLError("Cashu mint confirmed that the melt was unpaid")

    try:
        quote = await run_mint_operation(
            lambda: wallet.get_melt_quote(melt_quote_resp.quote),
            op_name="reconcile_lnurl_melt_quote",
            mint_url=str(wallet.url),
            retry_timeouts=False,
            # Reconciliation must bypass the cooldown opened by this failure.
            allow_during_cooldown=True,
        )
    except Exception as reconciliation_error:
        raise MeltOutcomeAmbiguousError(
            "Melt outcome is ambiguous; quote reconciliation failed and proofs "
            "must not be retried"
        ) from reconciliation_error

    if quote is not None and quote.state == MeltQuoteState.paid:
        return final_amount
    if quote is not None and quote.state == MeltQuoteState.unpaid:
        # A just-dispatched quote can briefly report unpaid before transitioning.
        try:
            await wallet.set_reserved_for_melt(
                proofs, reserved=True, quote_id=melt_quote_resp.quote
            )
        except Exception as reservation_error:
            raise MeltOutcomeAmbiguousError(
                "Melt outcome is ambiguous and its proof reservation could not "
                "be restored; proofs must not be retried"
            ) from reservation_error
        raise MeltOutcomeAmbiguousError(
            "Melt outcome is ambiguous; an immediate unpaid state is not final"
        ) from melt_error

    state = getattr(getattr(quote, "state", None), "value", "unknown")
    raise MeltOutcomeAmbiguousError(
        f"Melt outcome is ambiguous; proofs must not be retried (quote_state={state})"
    ) from melt_error
