import asyncio
import fcntl
import os
import re
import time
import typing
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncGenerator, TypedDict

import httpx
from cashu.core.base import MeltQuote, MeltQuoteState, MintQuote, Proof, Token
from cashu.core.mint_info import MintInfo as _CashuMintInfo
from cashu.wallet.helpers import deserialize_token_from_string
from cashu.wallet.wallet import Wallet as _CashuWallet
from pydantic_core import PydanticUndefined
from sqlmodel import col, select, update

from .core import db, get_logger
from .core.db import store_cashu_transaction_with_retry as store_cashu_transaction
from .core.settings import settings
from .mint import (
    MINT_TRANSPORT_COOLDOWN_SECONDS,
    MINT_TRANSPORT_EXCEPTIONS,
    MintRateGuard,
    MintRateLimitedError,
    fail_fast_mint_operations,
    is_mint_rate_limited,
    mint_cooldown_reason,
    mint_cooldown_remaining,
    run_mint_operation,
)
from .payment.lnurl import raw_send_to_lnurl

# Backwards-compatible aliases for callers/tests that imported the former
# wallet-local policy. Production modules use the public routstr.mint API.
_MintRateGuard = MintRateGuard
_mint_operation = run_mint_operation
_mint_cooldown_remaining = mint_cooldown_remaining
_mint_cooldown_reason = mint_cooldown_reason
_is_mint_rate_limited = is_mint_rate_limited

# cashu still declares Optional[X] without explicit defaults on MintInfo.
# Under pydantic v2 those are required, but real mints omit many of them.
# Default Optional fields to None at import time so balance fetches don't 422.
for _name, _field in _CashuMintInfo.model_fields.items():
    _annot = _field.annotation
    _is_optional = typing.get_origin(_annot) is typing.Union and type(
        None
    ) in typing.get_args(_annot)
    if _is_optional and _field.default is PydanticUndefined:
        _field.default = None
_CashuMintInfo.model_rebuild(force=True)

logger = get_logger(__name__)

# Preserve the real scheduler yield even when payout tests patch asyncio.sleep.
_scheduler_sleep = asyncio.sleep
_WALLET_OPERATION_LOCK = Path(".wallet") / ".routstr-operation.lock"
_wallet_operation_depth: ContextVar[int] = ContextVar(
    "wallet_operation_depth", default=0
)


@asynccontextmanager
async def wallet_operation_guard() -> AsyncGenerator[None, None]:
    """Serialize proof mutation and owner payout across local worker processes."""
    depth = _wallet_operation_depth.get()
    if depth:
        token = _wallet_operation_depth.set(depth + 1)
        try:
            yield
        finally:
            _wallet_operation_depth.reset(token)
        return

    _WALLET_OPERATION_LOCK.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(_WALLET_OPERATION_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    depth_token = None
    try:
        while not acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                await _scheduler_sleep(0.05)
        depth_token = _wallet_operation_depth.set(1)
        async with fail_fast_mint_operations():
            yield
    finally:
        if depth_token is not None:
            _wallet_operation_depth.reset(depth_token)
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _sats_to_msats(amount: int) -> int:
    return amount * 1000


def _msats_to_sats(amount: int) -> int:
    return amount // 1000


def _msats_to_sats_ceil(amount: int) -> int:
    """Round liabilities up so fractional sats are never treated as owner funds."""
    return (amount + 999) // 1000


def _mints_to_inspect() -> list[str]:
    """Return configured mints plus the primary mint, without duplicates."""
    mint_urls = list(settings.cashu_mints)
    if settings.primary_mint and settings.primary_mint not in mint_urls:
        mint_urls.append(settings.primary_mint)
    return mint_urls


class Wallet(_CashuWallet):
    """Cashu adapter that preserves HTTP 429 for Routstr's mint policy."""

    @staticmethod
    def raise_on_error_request(resp: httpx.Response) -> None:
        if resp.status_code == 429:
            raise MintRateLimitedError(
                "Cashu mint rate limited",
                request=resp.request,
                response=resp,
            )
        _CashuWallet.raise_on_error_request(resp)


class MintConnectionError(Exception):
    """The mint could not be reached (network transport failure).

    Maps to a 503, not a 4xx: the token is fine, the mint is just unavailable.
    """


class SourceMintConnectionError(MintConnectionError):
    """The mint that issued the incoming proofs cannot be reached."""


class TokenConsumedError(Exception):
    """A failure that happened AFTER the token's proofs were spent (melt
    succeeded, or redemption already returned) — e.g. minting on the primary
    mint or the DB credit then failed.

    Non-retryable: the same token will not work again. Seals the cause chain so
    a transport error underneath is never re-surfaced as a retryable
    mint_unreachable.
    """


def is_source_mint_connection_error(error: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, SourceMintConnectionError):
            return True
        current = current.__cause__ or current.__context__
    return False


def is_mint_connection_error(error: BaseException) -> bool:
    """True if ``error`` (or anything in its cause/context chain) is a mint
    transport failure. Walks the chain because some sites re-raise transport
    errors wrapped in ValueError/MintConnectionError; matches on TYPE, not text.
    """
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, TokenConsumedError):
            # Sealed: the token was already spent, so whatever transport error
            # sits underneath must not make this look retryable.
            return False
        if isinstance(current, MintConnectionError):
            return True
        if isinstance(current, MINT_TRANSPORT_EXCEPTIONS):
            return True
        current = current.__cause__ or current.__context__
    return False


# Redemption ``code`` values whose token is spent/consumed/unusable — the
# X-Cashu path must NOT echo the original token for these (echoing invites a
# retry with a token that can never succeed again).
SPENT_TOKEN_CODES: frozenset[str] = frozenset(
    {
        "cashu_token_already_spent",
        "cashu_token_consumed",
        "cashu_token_zero_value",
        "internal_error",
    }
)


def classify_redemption_error(
    error: Exception,
) -> tuple[str, int, str, str] | None:
    """Map a token-redemption failure to ``(type, status, message, code)``.

    Single source of truth for every endpoint that redeems a token (bearer,
    X-Cashu, top-up) so the same failure yields the same taxonomy everywhere.
    ``type`` and ``code`` are stable client contract; ``message`` is sanitized
    (raw error text stays in logs). Returns None for an unclassified internal
    fault — the caller emits a generic 500.
    """
    if isinstance(error, TokenConsumedError):
        return (
            "token_consumed",
            500,
            "Token was redeemed but could not be credited; do not retry",
            "cashu_token_consumed",
        )
    if is_source_mint_connection_error(error):
        return (
            "mint_unreachable",
            503,
            "The mint that issued this Cashu token is unreachable; the token cannot be redeemed at another mint",
            "cashu_source_mint_unreachable",
        )
    if is_mint_rate_limited(error):
        return (
            "mint_rate_limited",
            503,
            "Cashu mint rate-limited; retry after cooldown",
            "cashu_mint_rate_limited",
        )
    if is_mint_connection_error(error):
        return (
            "mint_unreachable",
            503,
            "Cashu mint is unreachable",
            "cashu_mint_unreachable",
        )
    lowered = str(error).lower()
    if "already spent" in lowered:
        return (
            "token_already_spent",
            400,
            "Cashu token already spent",
            "cashu_token_already_spent",
        )
    if (
        "insufficient" in lowered
        or "melt fee" in lowered
        or "exceed token amount" in lowered
        or "estimate fees" in lowered
    ):
        return (
            "mint_error",
            422,
            "Token value is too small to cover swap fees",
            "cashu_token_swap_fees_exceed_amount",
        )
    if "failed to melt" in lowered:
        return (
            "mint_error",
            422,
            "Failed to swap token from foreign mint",
            "cashu_foreign_mint_swap_failed",
        )
    if ("invalid" in lowered or "decode" in lowered) and "token" in lowered:
        # Anchored to "token" so internal faults whose text merely contains
        # "invalid"/"decode" fall through to the 500 branch, not a token error.
        return (
            "invalid_token",
            400,
            "Invalid Cashu token",
            "invalid_cashu_token",
        )
    if "must be positive" in lowered or "yielded no value" in lowered:
        # Redeemed to <= 0 (empty/dust token, or value fully consumed by fees).
        # Consumed, so non-retryable, but its own code — not the generic bucket.
        return (
            "cashu_error",
            400,
            "Failed to redeem Cashu token: token yielded no value",
            "cashu_token_zero_value",
        )
    if isinstance(error, ValueError):
        return (
            "cashu_error",
            400,
            "Failed to redeem Cashu token",
            "cashu_token_redemption_failed",
        )
    return None


async def get_balance(unit: str) -> int:
    wallet = await get_wallet(settings.primary_mint, unit)
    return wallet.available_balance.amount


async def _expand_short_keysets(wallet: "_CashuWallet", proofs: list[Proof]) -> None:
    """Expand short NUT-02 v2 keyset ids (e.g. minibits tokens) to full 66-char
    ids in place; the mint won't accept a short id on melt/swap."""
    short_ids = {
        p.id
        for p in proofs
        if isinstance(getattr(p, "id", None), str) and 0 < len(p.id) < 66
    }
    if not short_ids:
        return
    if not wallet.keysets:
        await run_mint_operation(
            lambda: wallet.load_mint(),
            op_name="load_mint_for_keyset_expansion",
            mint_url=wallet.url,
            retry_timeouts=False,
        )
    try:
        await wallet._expand_short_keyset_ids(proofs)
    except KeyError as e:
        raise ValueError(
            "Token carries a short keyset id that cannot be mapped to a "
            f"mint keyset (NUT-02 v2 migration): {e}"
        ) from e


async def _redeem_same_mint(
    wallet: Wallet, token_obj: Token
) -> tuple[int, str, str]:  # amount, unit, mint_url
    """Redeem proofs at their own issuing mint (no cross-mint swap).

    split() re-mints the incoming proofs into fresh ones we own so the sender
    can't double-spend them. With include_fees=True the mint deducts its NUT-02
    per-proof input fee, so we end up holding only `amount - input_fees`. Credit
    that, not the face value, or routstr over-credits the user and its wallet
    drifts insolvent.
    """
    try:
        await run_mint_operation(
            lambda: wallet.load_mint(keyset_id=token_obj.keysets[0]),
            op_name="redeem_load_mint",
            mint_url=token_obj.mint,
        )
    except Exception as error:
        if is_mint_connection_error(error):
            logger.warning(
                "Same-mint redemption failed before swap dispatch",
                extra={
                    "event": "cashu_same_mint_redemption_failed",
                    "source_mint": token_obj.mint,
                    "source_unit": token_obj.unit,
                    "source_amount": token_obj.amount,
                    "cross_mint_fallback_attempted": False,
                    "action": "retry_with_token_from_another_mint",
                    "error": str(error),
                    "error_type": type(error).__name__,
                },
            )
            raise SourceMintConnectionError(
                "Issuing Cashu mint is unreachable"
            ) from error
        raise

    # token_obj.proofs rebuilds fresh Proof objects on every access, so
    # capture it once and reuse below — otherwise _expand_short_keysets'
    # in-place id mutation gets silently discarded.
    proofs = token_obj.proofs
    await _expand_short_keysets(wallet, proofs)

    wallet.verify_proofs_dleq(proofs)
    input_fees = wallet.get_fees_for_proofs(proofs)
    try:
        await run_mint_operation(
            lambda: wallet.split(proofs=proofs, amount=0, include_fees=True),
            op_name="redeem_split",
            mint_url=token_obj.mint,
            retry_timeouts=False,
        )
    except Exception as error:
        if isinstance(error, httpx.ConnectError):
            raise SourceMintConnectionError(
                "Issuing Cashu mint is unreachable"
            ) from error
        if is_mint_connection_error(error):
            logger.critical(
                "Same-mint swap outcome is ambiguous; sealing source token",
                extra={
                    "event": "cashu_same_mint_redemption_ambiguous",
                    "source_mint": token_obj.mint,
                    "source_unit": token_obj.unit,
                    "source_amount": token_obj.amount,
                    "action": "manual_reconciliation_required",
                    "error": str(error),
                    "error_type": type(error).__name__,
                },
            )
            raise TokenConsumedError(
                "Same-mint swap outcome is ambiguous; reconciliation required"
            ) from error
        raise

    return int(token_obj.amount) - input_fees, token_obj.unit, token_obj.mint


async def recieve_token(
    token: str,
    destination_mint: str | None = None,
    destination_unit: str | None = None,
) -> tuple[int, str, str]:  # amount, unit, mint_url
    """Redeem a token while serializing all wallet proof mutation."""
    async with wallet_operation_guard():
        return await _recieve_token_locked(token, destination_mint, destination_unit)


async def _recieve_token_locked(
    token: str,
    destination_mint: str | None = None,
    destination_unit: str | None = None,
) -> tuple[int, str, str]:
    token_obj = deserialize_token_from_string(token)
    if len(token_obj.keysets) > 1:
        raise ValueError("Multiple keysets per token currently not supported")

    destinations = (
        [destination_mint]
        if destination_mint is not None
        else list(dict.fromkeys([settings.primary_mint, *settings.cashu_mints]))
    )
    output_unit = (
        token_obj.unit if token_obj.mint in destinations else settings.primary_mint_unit
    )
    if destination_unit is not None and output_unit != destination_unit:
        raise ValueError(
            "Cashu token unit does not match the API key liability unit: "
            f"expected {destination_unit}, got {output_unit}"
        )

    wallet = await get_wallet(token_obj.mint, token_obj.unit, load=False)
    wallet.keyset_id = token_obj.keysets[0]
    if token_obj.mint not in destinations:
        logger.info(
            "Cashu cross-mint swap required",
            extra={
                "event": "cashu_swap_started",
                "source_mint": token_obj.mint,
                "source_unit": token_obj.unit,
                "source_amount": token_obj.amount,
                "destination_candidates": destinations,
            },
        )
        return await swap_to_trusted_mint(
            token_obj, wallet, destination_mints=destinations
        )

    logger.info(
        "Trying same-mint Cashu redemption",
        extra={
            "event": "cashu_same_mint_redemption",
            "source_mint": token_obj.mint,
            "source_unit": token_obj.unit,
            "source_amount": token_obj.amount,
            "cross_mint_fallback_on_connection_failure": False,
        },
    )
    return await _redeem_same_mint(wallet, token_obj)


async def send(amount: int, unit: str, mint_url: str | None = None) -> tuple[int, str]:
    """Create a token from the preferred mint or another funded trusted mint."""
    async with wallet_operation_guard():
        return await _send_locked(amount, unit, mint_url)


async def _send_locked(
    amount: int, unit: str, mint_url: str | None = None
) -> tuple[int, str]:
    effective_mint_url = await find_trusted_mint_with_funds(
        amount, unit, mint_url, force_reload=True
    )
    wallet = await get_wallet(effective_mint_url, unit)
    proofs = get_proofs_per_mint_and_unit(
        wallet, effective_mint_url, unit, not_reserved=True
    )
    proofs_for_mint = sum(proof.amount for proof in proofs)
    all_proofs = get_proofs_per_mint_and_unit(wallet, effective_mint_url, unit)
    reserved_for_mint = sum(p.amount for p in all_proofs if p.reserved)

    all_mint_urls = list({k.mint_url for k in wallet.keysets.values()})
    proof_summary = {
        f"{k.mint_url}/{k.unit.name}": sum(
            p.amount for p in wallet.proofs if p.id == k.id
        )
        for k in wallet.keysets.values()
    }
    # Show ALL proofs in DB by keyset_id, regardless of whether the loaded wallet
    # knows about that keyset. This reveals proofs orphaned under stale keysets.
    raw_proofs_by_keyset: dict[str, int] = {}
    for p in wallet.proofs:
        raw_proofs_by_keyset[p.id] = raw_proofs_by_keyset.get(p.id, 0) + p.amount
    logger.info(
        f"send: proof inventory | mint={effective_mint_url} unit={unit} amount={amount} "
        f"primary_mint={settings.primary_mint} liquid_proofs_for_mint={proofs_for_mint} "
        f"reserved_proofs_for_mint={reserved_for_mint} "
        f"all_mints={all_mint_urls} by_keyset={proof_summary} "
        f"raw_proofs_by_keyset_id={raw_proofs_by_keyset} "
        f"total_wallet_proofs={sum(p.amount for p in wallet.proofs)}"
    )

    # Reserve proofs only after serialization succeeds — if serialize_proofs or
    # swap_to_send fails mid-way, proofs stay unreserved so dashboard balance
    # doesn't go negative.
    send_proofs, _ = await wallet.select_to_send(
        proofs, amount, set_reserved=False, include_fees=False
    )
    try:
        token = await wallet.serialize_proofs(
            send_proofs, include_dleq=False, legacy=False, memo=None
        )
    except Exception:
        await wallet.set_reserved_for_send(send_proofs, reserved=False)
        raise
    await wallet.set_reserved_for_send(send_proofs, reserved=True)
    return amount, token


async def send_token(amount: int, unit: str, mint_url: str | None = None) -> str:
    _, token = await send(amount, unit, mint_url)
    return token


class Bolt11PaymentNotAttempted(Exception):
    """The invoice was definitively not paid, so the attempt can be retried.

    Raised only where the mint's own answer rules out a settlement: coin
    selection never reached ``melt``, or ``melt`` returned an explicit unpaid
    state. Any proofs reserved along the way are released before this is
    raised.
    """


class Bolt11PaymentAmbiguous(Exception):
    """The payment may or may not have settled, so it must not be retried.

    Raised when ``melt`` errored, timed out, or came back pending. The selected
    proofs stay reserved: the mint may still complete the payment with them,
    and spending them elsewhere would be a double spend.
    """


@dataclass
class Bolt11PaymentPlan:
    invoice: str
    wallet: Wallet
    proofs: list[Proof]
    quote: MeltQuote
    mint_url: str
    unit: str

    @property
    def invoice_amount_sats(self) -> int:
        amount = int(self.quote.amount)
        return amount if self.unit == "sat" else (amount + 999) // 1000

    @property
    def maximum_spend_sats(self) -> int:
        maximum = (
            int(self.quote.amount)
            + int(self.quote.fee_reserve)
            + int(self.wallet.get_fees_for_proofs(self.proofs))
        )
        return maximum if self.unit == "sat" else (maximum + 999) // 1000


async def _owner_balance_for_mint_and_unit(
    mint_url: str, unit: str, proofs_balance: int
) -> int:
    """Return spendable node-owned funds without crossing user liabilities."""
    async with db.create_session() as session:
        # Refund mint is a preference, not funding provenance. Mirror payout's
        # conservative rule and protect the full liability at every mint.
        user_liability = await db.total_user_liability(session)
    # API-key balances are stored in msats. Cashu ``sat`` proofs are not.
    if unit == "sat":
        user_liability = _msats_to_sats_ceil(user_liability)
    return max(0, proofs_balance - user_liability)


async def maximum_owner_cashu_balance_sats() -> int:
    """Return the largest conservatively owner-funded mint/unit balance."""
    details, _, _, _ = await fetch_all_balances()
    async with db.create_session() as session:
        liability_sats = _msats_to_sats_ceil(await db.total_user_liability(session))
    balances = [
        (
            detail["wallet_balance"]
            if detail["unit"] == "sat"
            else _msats_to_sats(detail["wallet_balance"])
        )
        - liability_sats
        for detail in details
        if not detail.get("error")
    ]
    return max([0, *balances])


async def prepare_bolt11_payment(invoice: str) -> Bolt11PaymentPlan:
    """Choose the sufficiently funded configured mint with most owner funds.

    Candidate discovery reads balances, user liabilities, and melt quotes. Coin
    selection, which may split proofs, is deferred until the winner is known.

    Runs under ``wallet_operation_guard``: the plan snapshots live proof state,
    which another worker process could otherwise mutate mid-read. Callers that
    go on to execute the plan should hold the guard across both calls so the
    snapshot stays valid.
    """
    async with wallet_operation_guard():
        return await _prepare_bolt11_payment(invoice)


async def _prepare_bolt11_payment(invoice: str) -> Bolt11PaymentPlan:
    mint_urls = list(dict.fromkeys([*settings.cashu_mints, settings.primary_mint]))
    candidates: list[tuple[int, Wallet, list[Proof], MeltQuote, str, str]] = []
    failures: list[dict[str, str]] = []
    evaluated = 0

    for mint_url in mint_urls:
        if not mint_url:
            continue
        for unit in ("sat", "msat"):
            try:
                # force_reload: the guard's flock only serializes access — a
                # cached wallet can still hold proof state from before another
                # process's reservation landed on disk.
                wallet = await get_wallet(mint_url, unit, force_reload=True)
                proofs = get_proofs_per_mint_and_unit(
                    wallet, mint_url, unit, not_reserved=True
                )
                proofs = await slow_filter_spend_proofs(proofs, wallet)
                proofs_balance = sum(proof.amount for proof in proofs)
                if proofs_balance <= 0:
                    evaluated += 1
                    continue

                quote = await wallet.melt_quote(invoice=invoice)
                evaluated += 1
                # select_to_send runs with include_fees=True, so the input fee
                # has to be part of sufficiency too. Without it a mint passes
                # this filter and then fails coin selection.
                required = (
                    quote.amount
                    + quote.fee_reserve
                    + wallet.get_fees_for_proofs(proofs)
                )
                owner_balance = await _owner_balance_for_mint_and_unit(
                    mint_url, unit, proofs_balance
                )
                if owner_balance < required:
                    continue
                owner_balance_msats = (
                    owner_balance * 1000 if unit == "sat" else owner_balance
                )
                candidates.append(
                    (owner_balance_msats, wallet, proofs, quote, mint_url, unit)
                )
            except Exception as e:
                failures.append({"mint_url": mint_url, "unit": unit, "error": str(e)})
                logger.debug(
                    "Cashu mint cannot fund BOLT11 invoice",
                    extra={"mint_url": mint_url, "unit": unit, "error": str(e)},
                )

    if not candidates:
        if failures:
            logger.warning(
                "No Cashu mint could fund the BOLT11 invoice",
                extra={"evaluated": evaluated, "failures": failures},
            )
        if evaluated == 0 and failures:
            raise RuntimeError("Every configured Cashu mint refused the payment")
        raise ValueError(
            "No configured Cashu mint has enough balance after user liabilities to pay invoice"
        )

    _, wallet, proofs, quote, mint_url, unit = max(candidates, key=lambda item: item[0])
    return Bolt11PaymentPlan(invoice, wallet, proofs, quote, mint_url, unit)


async def execute_bolt11_payment(plan: Bolt11PaymentPlan) -> tuple[int, str, str]:
    """Execute a prepared payment, separating retryable from ambiguous failure.

    Raises ``Bolt11PaymentNotAttempted`` when the invoice provably did not
    settle, and ``Bolt11PaymentAmbiguous`` when the outcome is unknown. Callers
    may safely retry the first and must never retry the second.

    Runs under ``wallet_operation_guard``: coin selection and reservation must
    not race another worker process spending the same proofs.
    """
    async with wallet_operation_guard():
        return await _execute_bolt11_payment(plan)


async def _execute_bolt11_payment(plan: Bolt11PaymentPlan) -> tuple[int, str, str]:
    # Select unreserved, mirroring send_token: a selection failure must not
    # strand proofs that were never handed to the mint.
    try:
        selected, _ = await plan.wallet.select_to_send(
            plan.proofs,
            plan.quote.amount + plan.quote.fee_reserve,
            set_reserved=False,
            include_fees=True,
        )
    except Exception as e:
        raise Bolt11PaymentNotAttempted(f"Coin selection failed: {e}") from e

    await plan.wallet.set_reserved_for_send(selected, reserved=True)

    try:
        result = await asyncio.wait_for(
            plan.wallet.melt(
                proofs=selected,
                invoice=plan.invoice,
                fee_reserve_sat=plan.quote.fee_reserve,
                quote_id=plan.quote.quote,
            ),
            timeout=60,
        )
    except BaseException as e:
        # The mint may still be settling with these proofs, so they must stay
        # reserved — but cashu's melt() un-reserves them itself on a mint
        # transport error, the exact ambiguous case. Re-reserve with the melt
        # quote id, not as a send: get_melt_quote() finds the proofs to settle
        # by melt_id, so a send-style reservation would strand them — paid
        # proofs never invalidated, unpaid ones never released. BaseException
        # includes task cancellation after the melt was submitted.
        try:
            await asyncio.shield(
                plan.wallet.set_reserved_for_melt(
                    selected, reserved=True, quote_id=plan.quote.quote
                )
            )
        except BaseException:
            logger.critical(
                "Could not re-reserve proofs after an ambiguous melt",
                extra={"mint_url": plan.mint_url, "quote_id": plan.quote.quote},
            )
        if isinstance(e, asyncio.CancelledError):
            raise
        raise Bolt11PaymentAmbiguous(f"Cashu melt did not return: {e}") from e

    raw_state = getattr(result, "state", None)
    state = str(raw_state).lower().rsplit(".", 1)[-1] if raw_state is not None else ""
    if state == "paid" or getattr(result, "paid", None) is True:
        change = getattr(result, "change", None) or []
        paid = sum(proof.amount for proof in selected) - sum(
            int(item.amount) for item in change
        )
        return paid, plan.mint_url, plan.unit

    if state == "unpaid":
        # The mint is telling us it did not pay, so the proofs are ours again.
        await plan.wallet.set_reserved_for_send(selected, reserved=False)
        raise Bolt11PaymentNotAttempted("Cashu mint reported the melt as unpaid")

    raise Bolt11PaymentAmbiguous(
        f"Cashu melt did not reach a final state: {state or 'unknown'}"
    )


async def check_bolt11_payment_status(mint_url: str, unit: str, quote_id: str) -> str:
    """Ask the mint what became of an earlier melt attempt.

    Returns ``"paid"``, ``"unpaid"``, ``"pending"``, or ``"unknown"``. This is
    the durable reconciliation path for an ambiguous payment: cashu's
    ``get_melt_quote`` also settles the wallet database — invalidating the
    proofs on ``paid`` and releasing their reservation on ``unpaid`` — so a
    caller that sees ``"unpaid"`` may safely retry with the same funds.

    Runs under ``wallet_operation_guard`` because of that side effect: it
    mutates proof state and must not race other processes' wallet operations.
    """
    async with wallet_operation_guard():
        return await _check_bolt11_payment_status_locked(mint_url, unit, quote_id)


async def _check_bolt11_payment_status_locked(
    mint_url: str, unit: str, quote_id: str
) -> str:
    """Check a melt quote while the caller holds ``wallet_operation_guard``."""
    try:
        wallet = await get_wallet(mint_url, unit, force_reload=True)
        quote = await wallet.get_melt_quote(quote_id)
    except Exception as e:
        logger.warning(
            "Could not query the mint for a melt quote's status",
            extra={"mint_url": mint_url, "quote_id": quote_id, "error": str(e)},
        )
        return "unknown"
    if quote is None:
        return "unknown"
    state = str(getattr(quote, "state", "")).lower().rsplit(".", 1)[-1]
    if state in ("paid", "unpaid", "pending"):
        return state
    return "unknown"


async def release_token_reservation(token: str) -> None:
    """Release a token that was created locally but never handed off."""
    async with wallet_operation_guard():
        token_obj = deserialize_token_from_string(token)
        wallet = await get_wallet(token_obj.mint, token_obj.unit, load=False)
        # This is a local wallet-DB refresh; reservation release must still work
        # while the mint is unavailable or cooling down.
        await wallet.load_proofs(reload=True)
        await wallet.set_reserved_for_send(token_obj.proofs, reserved=False)

        secrets = {proof.secret for proof in token_obj.proofs}
        for proof in token_obj.proofs:
            proof.reserved = False
        for proof in wallet.proofs:
            if proof.secret in secrets:
                proof.reserved = False


def token_mint_url(token: str, fallback: str | None = None) -> str:
    try:
        return str(deserialize_token_from_string(token).mint)
    except Exception:
        if fallback is None:
            raise
        return fallback


async def find_trusted_mint_with_funds(
    amount: int,
    unit: str,
    preferred_mint: str | None = None,
    *,
    force_reload: bool = False,
) -> str:
    """Choose a trusted mint that can cover a refund without waiting on cooldown."""
    trusted = list(dict.fromkeys([settings.primary_mint, *settings.cashu_mints]))
    candidates: list[str] = []
    if preferred_mint in trusted:
        candidates.append(preferred_mint)
    candidates.extend(mint for mint in trusted if mint not in candidates)

    balances: dict[str, int] = {}
    for mint_url in candidates:
        if mint_cooldown_remaining(mint_url) > 0:
            continue
        try:
            wallet = await get_wallet(
                mint_url,
                unit,
                retry_on_rate_limit=False,
                force_reload=force_reload,
            )
        except Exception as error:
            if is_mint_connection_error(error) or is_mint_rate_limited(error):
                balances[mint_url] = 0
                continue
            raise

        proofs = get_proofs_per_mint_and_unit(wallet, mint_url, unit, not_reserved=True)
        balances[mint_url] = sum(proof.amount for proof in proofs)
        if balances[mint_url] >= amount:
            return mint_url

    raise ValueError(
        f"No trusted mint has {amount} {unit} available; balances={balances}"
    )


# A foreign mint's fee_reserve is a non-binding estimate (NUT-05): the mint may
# demand more when re-quoting or at melt execution. Instead of padding the
# estimate with a safety buffer (which strands the margin at the foreign mint
# on every swap), the swap retries with the amount recomputed from the fees the
# mint actually demands, up to this many attempts.
_MAX_SWAP_ATTEMPTS = 3

_MINT_ERROR_CODE_RE = re.compile(r"\(Code: (\d+)\)")
_MELT_SHORTFALL_RE = re.compile(r"Provided: (\d+), needed: (\d+)")

# Insufficient-melt-inputs failures differ across mint implementations. 11005 is
# the registered "Transaction is not balanced" code (cdk), specific enough to
# trust on the code alone. 11000 is nutshell's generic, unregistered
# TransactionError covering many unrelated failures, so it only counts as a fee
# shortfall alongside the "not enough inputs" detail text. With no code suffix at
# all, that same text is the only signal.


def _net_minted_amount(amount_msat: int, token_unit: str, fees: int) -> int:
    """
    Convert the token value minus fees (given in the token unit) into an
    amount in the primary mint's unit.
    """
    fee_msat = _sats_to_msats(fees) if token_unit == "sat" else fees
    remaining_msat = amount_msat - fee_msat
    if settings.primary_mint_unit == "sat":
        return _msats_to_sats(remaining_msat)
    return int(remaining_msat)


def _melt_definitively_failed(error: Exception) -> bool:
    """Return whether the mint authoritatively rejected the Lightning payment.

    Cashu releases the reserved proofs for these responses, so the token remains
    reusable. Transport failures and unknown errors are deliberately excluded:
    after dispatch their payment outcome may still be pending or paid.
    """
    message = str(error).strip()
    return message.lower() == "could not pay invoice." or "(Code: 20004)" in message


def _melt_insufficient_shortfall(error: Exception) -> int | None:
    """
    Classify a melt failure: return the observed shortfall (in the token unit)
    when the mint rejected the inputs as insufficient, or None when the failure
    is unrelated to fees and must not be retried (e.g. a Lightning payment
    failure, where a smaller invoice would not help).

    Cashu errors carry no structured amounts (NUT-00 defines only detail/code,
    flattened to "Mint Error: <detail> (Code: <code>)" by cashu-py), so the
    classification uses the code and the shortfall must be inferred: the
    "Provided: X, needed: Y" amounts are nutshell-specific free text and only
    refine the shortfall when present; otherwise shrink one unit at a time.
    """
    message = str(error)
    code_match = _MINT_ERROR_CODE_RE.search(message)
    code = code_match.group(1) if code_match is not None else None
    has_shortfall_text = "not enough inputs" in message.lower()

    match code:
        case "11005":  # registered TransactionUnbalanced: trust the code
            pass
        case "11000" if has_shortfall_text:  # generic nutshell error: needs the text
            pass
        case None if has_shortfall_text:  # no code suffix: text is the only signal
            pass
        case _:  # other codes, a bare 11000, or no signal: must not retry
            return None

    amounts = _MELT_SHORTFALL_RE.search(message)
    if amounts is not None:
        provided, needed = int(amounts.group(1)), int(amounts.group(2))
        if needed > provided:
            return needed - provided
    return 1


def _trusted_destination_candidates(
    candidates: list[str] | None = None,
) -> list[str]:
    trusted = list(dict.fromkeys([settings.primary_mint, *settings.cashu_mints]))
    if candidates is None:
        return trusted
    selected = list(dict.fromkeys(candidates))
    untrusted = [mint_url for mint_url in selected if mint_url not in trusted]
    if untrusted:
        raise ValueError(f"Untrusted destination mint: {untrusted[0]}")
    if not selected:
        raise ValueError("At least one trusted destination mint is required")
    return selected


async def _request_mint_with_fallback(
    amount: int,
    *,
    op_name: str,
    primary_wallet: Wallet | None = None,
    destination_mints: list[str] | None = None,
) -> tuple[Wallet, str, MintQuote]:
    """Try request_mint on the primary mint, fall back to other trusted mints
    on transport or rate-limit failure. Returns the wallet, mint_url, and quote.

    Guards against amount <= 0: the cashu library's PostMintQuoteRequest
    enforces ``amount > 0`` (Pydantic Field(gt=0)), so passing 0 raises a
    cryptic validation error deep in the stack.  Fail fast with context.
    """
    if amount <= 0:
        raise ValueError(
            f"_request_mint_with_fallback({op_name}): amount must be > 0, got {amount}. "
            f"Token value is too small after fee deduction or unit conversion."
        )
    candidates = _trusted_destination_candidates(destination_mints)
    logger.info(
        "Trying trusted destination mints",
        extra={
            "event": "cashu_destination_candidates",
            "op_name": op_name,
            "amount": amount,
            "unit": settings.primary_mint_unit,
            "candidates": candidates,
        },
    )
    tried: list[str] = []
    for candidate_index, mint_url in enumerate(candidates, start=1):
        cooldown = mint_cooldown_remaining(mint_url)
        if cooldown > 0:
            tried.append(f"{mint_url}: cooling down")
            logger.warning(
                "Skipping unavailable destination mint",
                extra={
                    "event": "cashu_destination_skipped",
                    "mint_url": mint_url,
                    "cooldown_seconds": round(cooldown, 2),
                    "op_name": op_name,
                    "candidate_index": candidate_index,
                    "candidate_count": len(candidates),
                },
            )
            continue
        logger.info(
            "Trying destination mint",
            extra={
                "event": "cashu_destination_attempt",
                "mint_url": mint_url,
                "op_name": op_name,
                "candidate_index": candidate_index,
                "candidate_count": len(candidates),
            },
        )
        try:
            if mint_url == settings.primary_mint and primary_wallet is not None:
                wallet = primary_wallet
            else:
                wallet = await get_wallet(
                    mint_url,
                    settings.primary_mint_unit,
                    retry_on_rate_limit=False,
                )
            quote = await run_mint_operation(
                lambda: wallet.request_mint(amount),
                op_name=op_name,
                mint_url=mint_url,
                retry_timeouts=False,
                retry_on_rate_limit=False,
            )
            logger.info(
                "Destination mint selected",
                extra={
                    "event": "cashu_destination_selected",
                    "mint_url": mint_url,
                    "op_name": op_name,
                    "candidate_index": candidate_index,
                    "fallback_used": candidate_index > 1,
                },
            )
            return wallet, mint_url, quote
        except Exception as error:
            tried.append(f"{mint_url}: {type(error).__name__}")
            connection_failure = is_mint_connection_error(error)
            rate_limited = is_mint_rate_limited(error)
            if not connection_failure and not rate_limited:
                raise
            if connection_failure:
                MintRateGuard.get(mint_url).apply_cooldown(
                    MINT_TRANSPORT_COOLDOWN_SECONDS, reason="unreachable"
                )
            logger.warning(
                "Destination mint failed",
                extra={
                    "event": "cashu_destination_failed",
                    "failed_mint": mint_url,
                    "error": str(error),
                    "error_type": type(error).__name__,
                    "connection_failure": connection_failure,
                    "rate_limited": rate_limited,
                    "tried": tried,
                    "op_name": op_name,
                    "candidate_index": candidate_index,
                    "candidate_count": len(candidates),
                },
            )
            continue
    logger.error(
        "All trusted destination mints failed",
        extra={
            "event": "cashu_destination_exhausted",
            "op_name": op_name,
            "amount": amount,
            "unit": settings.primary_mint_unit,
            "candidates": candidates,
            "tried": tried,
        },
    )
    raise MintConnectionError(f"All mints failed for {op_name}: {tried}")


async def _calculate_swap_amount(
    amount_msat: int,
    token_unit: str,
    token_mint_url: str,
    token_wallet: Wallet,
    primary_wallet: Wallet | None,
    proofs: list,
    destination_mints: list[str] | None = None,
) -> int:
    """
    Calculate the amount to mint on the primary mint after accounting for
    melt fees and NUT-02 input fees on the foreign mint.
    """
    if settings.primary_mint_unit == "sat":
        receive_amount = _msats_to_sats(amount_msat)
    else:
        receive_amount = amount_msat

    if token_mint_url == settings.primary_mint:
        logger.info(
            "swap_to_trusted_mint: skipping fee estimation (same mint)",
            extra={"minted_amount": receive_amount},
        )
        return int(receive_amount)

    # The cashu library's PostMintQuoteRequest enforces amount > 0 (Pydantic
    # Field(gt=0)).  When the token's face value in the primary mint's unit
    # truncates to 0 (e.g. < 1000 msat with a "sat" primary unit), calling
    # request_mint(0) raises a validation error that is cryptic in production
    # logs.  Guard early with full diagnostic context instead.
    if receive_amount <= 0:
        logger.error(
            "swap_to_trusted_mint: receive_amount is zero or negative, cannot estimate fees",
            extra={
                "amount_msat": amount_msat,
                "token_unit": token_unit,
                "token_mint_url": token_mint_url,
                "primary_mint": settings.primary_mint,
                "primary_mint_unit": settings.primary_mint_unit,
                "receive_amount": receive_amount,
            },
        )
        raise ValueError(
            f"Token amount ({amount_msat} msat, unit={token_unit}) is too small to "
            f"swap to primary mint ({settings.primary_mint}, unit={settings.primary_mint_unit}): "
            f"receive_amount={receive_amount}. Minimum 1 {settings.primary_mint_unit} required."
        )

    logger.info(
        "swap_to_trusted_mint: estimating fees",
        extra={
            "dummy_amount": receive_amount,
            "unit": settings.primary_mint_unit,
            "token_mint_url": token_mint_url,
            "primary_mint": settings.primary_mint,
            "amount_msat": amount_msat,
        },
    )

    stage = "destination_fee_quote"
    try:
        _, _, dummy_mint_quote = await _request_mint_with_fallback(
            receive_amount,
            op_name="swap_fee_est_mint_quote",
            primary_wallet=primary_wallet,
            destination_mints=destination_mints,
        )
        stage = "source_fee_quote"
        dummy_melt_quote = await run_mint_operation(
            lambda: token_wallet.melt_quote(dummy_mint_quote.request),
            op_name="swap_fee_est_melt_quote",
            mint_url=token_mint_url,
        )

        fee_reserve = dummy_melt_quote.fee_reserve
        input_fees = token_wallet.get_fees_for_proofs(proofs)
        total_fees = fee_reserve + input_fees
        minted_amount = _net_minted_amount(amount_msat, token_unit, total_fees)

        if minted_amount <= 0:
            raise ValueError(f"Fees ({total_fees} {token_unit}) exceed token amount")

        logger.info(
            "swap_to_trusted_mint: fee estimation result",
            extra={
                "token_amount_sat": _msats_to_sats(amount_msat),
                "estimated_fee": total_fees,
                "estimated_fee_unit": token_unit,
                "input_fees": input_fees,
                "minted_amount": minted_amount,
                "minted_unit": settings.primary_mint_unit,
                "fee_reserve": fee_reserve,
                "token_mint_url": token_mint_url,
                "primary_mint": settings.primary_mint,
            },
        )
        return minted_amount

    except Exception as e:
        logger.error(
            "Cashu swap fee estimation failed",
            extra={
                "event": "cashu_swap_fee_estimation_failed",
                "stage": stage,
                "error": str(e),
                "error_type": type(e).__name__,
                "amount_msat": amount_msat,
                "token_unit": token_unit,
                "token_mint_url": token_mint_url,
                "primary_mint": settings.primary_mint,
                "primary_mint_unit": settings.primary_mint_unit,
                "receive_amount": receive_amount,
            },
        )
        if is_mint_connection_error(e):
            if stage == "source_fee_quote":
                logger.error(
                    "Source mint is unreachable; destination fallback cannot spend its proofs",
                    extra={
                        "event": "cashu_source_mint_unreachable",
                        "source_mint": token_mint_url,
                        "stage": stage,
                        "fallback_possible": False,
                        "reason": "cashu_proofs_are_bound_to_the_issuing_mint",
                    },
                )
                raise SourceMintConnectionError(
                    "Issuing Cashu mint is unreachable"
                ) from e
            raise MintConnectionError("Cashu mint is unreachable") from e
        raise ValueError(f"Failed to estimate fees: {e}") from e


async def _reconcile_ambiguous_melt(
    wallet: Wallet, quote_id: str, proofs: list[Proof]
) -> bool:
    """Confirm a dispatched melt is paid or conservatively mark it ambiguous.

    A PAID quote is authoritative and does not require a proof-state lookup.
    Every other immediate snapshot remains unsafe to retry: an in-flight
    Lightning payment can still move UNPAID/UNSPENT to PENDING or PAID after the
    cancelled HTTP request returns.
    """
    try:
        quote = await run_mint_operation(
            lambda: wallet.get_melt_quote(quote_id),
            op_name="reconcile_swap_melt_quote",
            mint_url=str(wallet.url),
            retry_timeouts=False,
        )
    except Exception as error:
        raise TokenConsumedError(
            "Source melt outcome is unknown; reconciliation required"
        ) from error

    if quote is not None and quote.state == MeltQuoteState.paid:
        return True

    try:
        proof_response = await run_mint_operation(
            lambda: wallet.check_proof_state(proofs),
            op_name="reconcile_swap_proofs",
            mint_url=str(wallet.url),
            retry_timeouts=False,
        )
        proof_states = [state.state.value for state in proof_response.states]
    except Exception:
        proof_states = []

    quote_state = getattr(getattr(quote, "state", None), "value", "unknown")
    raise TokenConsumedError(
        "Source melt outcome is ambiguous; reconciliation required "
        f"(quote_state={quote_state}, proof_states={proof_states})"
    )


async def _confirm_melt_paid(
    wallet: Wallet, quote_id: str, proofs: list[Proof], response: object
) -> bool:
    """Accept a melt response only when PAID is explicit or reconciled."""
    if getattr(response, "state", None) == MeltQuoteState.paid:
        return True
    return await _reconcile_ambiguous_melt(wallet, quote_id, proofs)


async def swap_to_trusted_mint(
    token_obj: Token,
    token_wallet: Wallet,
    *,
    destination_mints: list[str] | None = None,
) -> tuple[int, str, str]:
    logger.info(
        "Starting Cashu cross-mint swap",
        extra={
            "event": "cashu_swap_started",
            "source_mint": token_obj.mint,
            "token_amount": token_obj.amount,
            "unit": token_obj.unit,
            "primary_mint": settings.primary_mint,
        },
    )
    # Ensure amount is an integer
    if not isinstance(token_obj.amount, int):
        token_amount = int(token_obj.amount)
    else:
        token_amount = token_obj.amount

    if token_obj.unit == "sat":
        amount_msat = _sats_to_msats(token_amount)
    elif token_obj.unit == "msat":
        amount_msat = token_amount
    else:
        raise ValueError("Invalid unit")
    destination_candidates = _trusted_destination_candidates(destination_mints)
    # If the token is already from an allowed destination, redeem it same-mint.
    # There's no melt/Lightning fee, but the mint's NUT-02 input fee still
    # applies; _redeem_same_mint accounts for it.
    if token_obj.mint in destination_candidates:
        logger.info(
            "swap_to_trusted_mint: token already on primary mint, skipping swap",
            extra={
                "mint": token_obj.mint,
                "amount": token_amount,
                "unit": token_obj.unit,
            },
        )
        return await _redeem_same_mint(token_wallet, token_obj)

    # token_obj.proofs rebuilds fresh Proof objects on every access, so
    # capture it once and reuse below — otherwise _expand_short_keysets'
    # in-place id mutation gets silently discarded.
    proofs = token_obj.proofs
    await _expand_short_keysets(token_wallet, proofs)

    primary_wallet: Wallet | None = None

    minted_amount = await _calculate_swap_amount(
        amount_msat,
        token_obj.unit,
        token_obj.mint,
        token_wallet,
        primary_wallet,
        proofs,
        destination_candidates,
    )

    # The estimate above is non-binding: the mint may demand a higher fee on the
    # real quote or reject the melt outright. Retry the quote/melt cycle with the
    # amount recomputed from the fees the mint actually demands.
    observed_extra_fee = 0
    attempt = 0
    dest_wallet = primary_wallet
    dest_mint_url = settings.primary_mint
    while True:
        attempt += 1
        if minted_amount <= 0:
            logger.error(
                "swap_to_trusted_mint: minted_amount is zero or negative before requesting quote",
                extra={
                    "minted_amount": minted_amount,
                    "attempt": attempt,
                    "foreign_mint": token_obj.mint,
                    "token_amount": token_amount,
                    "token_unit": token_obj.unit,
                    "amount_msat": amount_msat,
                    "observed_extra_fee": observed_extra_fee,
                    "primary_mint": settings.primary_mint,
                },
            )
            raise ValueError(
                f"Cannot swap token ({token_amount} {token_obj.unit}) from {token_obj.mint}: "
                f"minted_amount={minted_amount} after fee deduction (attempt {attempt})"
            )
        dest_wallet, dest_mint_url, mint_quote = await _request_mint_with_fallback(
            minted_amount,
            op_name="swap_request_mint",
            primary_wallet=primary_wallet,
            destination_mints=destination_candidates,
        )
        logger.info(
            "swap_to_trusted_mint: mint quote received",
            extra={
                "mint_quote_id": mint_quote.quote,
                "attempt": attempt,
                "dest_mint": dest_mint_url,
            },
        )

        logger.info(
            "Requesting melt quote from source mint",
            extra={
                "event": "cashu_source_melt_quote_attempt",
                "source_mint": token_obj.mint,
                "destination_mint": dest_mint_url,
                "attempt": attempt,
            },
        )
        try:
            melt_quote = await run_mint_operation(
                lambda: token_wallet.melt_quote(mint_quote.request),
                op_name="swap_melt_quote",
                mint_url=token_obj.mint,
            )
        except Exception as error:
            if is_mint_connection_error(error):
                logger.error(
                    "Source mint is unreachable; destination fallback cannot spend its proofs",
                    extra={
                        "event": "cashu_source_mint_unreachable",
                        "source_mint": token_obj.mint,
                        "destination_mint": dest_mint_url,
                        "stage": "source_melt_quote",
                        "error": str(error),
                        "error_type": type(error).__name__,
                        "attempt": attempt,
                    },
                )
                raise SourceMintConnectionError(
                    "Issuing Cashu mint is unreachable"
                ) from error
            raise
        input_fees = token_wallet.get_fees_for_proofs(proofs)
        total_needed = melt_quote.amount + melt_quote.fee_reserve + input_fees
        logger.info(
            "swap_to_trusted_mint: melt quote received",
            extra={
                "melt_quote_id": melt_quote.quote,
                "melt_amount": melt_quote.amount,
                "melt_fee_reserve": melt_quote.fee_reserve,
                "input_fees": input_fees,
                "total_needed": total_needed,
                "token_amount": token_amount,
                "attempt": attempt,
            },
        )

        if total_needed > token_amount:
            recomputed = _net_minted_amount(
                amount_msat,
                token_obj.unit,
                melt_quote.fee_reserve + input_fees + observed_extra_fee,
            )
            if attempt >= _MAX_SWAP_ATTEMPTS or recomputed <= 0:
                logger.warning(
                    "swap_to_trusted_mint: insufficient token amount for melt fees",
                    extra={
                        "token_amount": token_amount,
                        "melt_amount": melt_quote.amount,
                        "melt_fee_reserve": melt_quote.fee_reserve,
                        "input_fees": input_fees,
                        "total_needed": total_needed,
                        "shortfall": total_needed - token_amount,
                        "attempts": attempt,
                    },
                )
                raise ValueError(
                    f"Token amount ({token_amount} {token_obj.unit}) is insufficient to cover "
                    f"melt fees. Needed: {total_needed} {token_obj.unit} "
                    f"(amount: {melt_quote.amount} + fee: {melt_quote.fee_reserve} + input_fees: {input_fees})"
                )
            logger.warning(
                "swap_to_trusted_mint: melt quote exceeds token amount, retrying",
                extra={
                    "total_needed": total_needed,
                    "token_amount": token_amount,
                    "retry_minted_amount": recomputed,
                    "attempt": attempt,
                },
            )
            minted_amount = recomputed
            continue

        try:
            melt_response = await run_mint_operation(
                lambda: token_wallet.melt(
                    proofs=proofs,
                    invoice=mint_quote.request,
                    fee_reserve_sat=melt_quote.fee_reserve,
                    quote_id=melt_quote.quote,
                ),
                op_name="swap_melt",
                mint_url=token_obj.mint,
                retry_timeouts=False,
            )
            await _confirm_melt_paid(
                token_wallet, melt_quote.quote, proofs, melt_response
            )
        except Exception as e:
            shortfall = _melt_insufficient_shortfall(e)
            if shortfall is None:
                if isinstance(e, TokenConsumedError):
                    raise
                if _melt_definitively_failed(e):
                    raise ValueError(
                        f"Failed to melt token from foreign mint {token_obj.mint}: {e}"
                    ) from e
                if is_mint_connection_error(e):
                    await _reconcile_ambiguous_melt(
                        token_wallet, melt_quote.quote, proofs
                    )
                    logger.info(
                        "Source melt reconciled as paid; minting on destination",
                        extra={
                            "event": "cashu_source_melt_reconciled_paid",
                            "source_mint": token_obj.mint,
                            "destination_mint": dest_mint_url,
                            "melt_quote_id": melt_quote.quote,
                        },
                    )
                    break
                raise TokenConsumedError(
                    "Source melt failed after dispatch; outcome requires reconciliation"
                ) from e

            observed_extra_fee += shortfall
            recomputed = _net_minted_amount(
                amount_msat,
                token_obj.unit,
                melt_quote.fee_reserve + input_fees + observed_extra_fee,
            )
            if attempt >= _MAX_SWAP_ATTEMPTS or recomputed <= 0:
                logger.error(
                    "swap_to_trusted_mint: melt failed",
                    extra={
                        "error": str(e),
                        "error_type": type(e).__name__,
                        "foreign_mint": token_obj.mint,
                        "token_amount": token_amount,
                        "melt_quote_id": melt_quote.quote,
                        "total_needed": total_needed,
                        "attempts": attempt,
                    },
                )
                raise ValueError(
                    f"Failed to melt token from foreign mint {token_obj.mint}: {e}"
                ) from e
            logger.warning(
                "swap_to_trusted_mint: mint demanded more than quoted at melt, retrying",
                extra={
                    "shortfall": shortfall,
                    "retry_minted_amount": recomputed,
                    "attempt": attempt,
                },
            )
            minted_amount = recomputed
            continue

        break

    logger.info(
        "Source melt succeeded; minting on destination",
        extra={
            "event": "cashu_destination_mint_attempt",
            "minted_amount": minted_amount,
            "mint_quote_id": mint_quote.quote,
            "dest_mint": dest_mint_url,
        },
    )

    await dest_wallet.load_proofs(reload=True)
    pre_mint_balance = dest_wallet.available_balance.amount
    try:
        _ = await run_mint_operation(
            lambda: dest_wallet.mint(minted_amount, quote_id=mint_quote.quote),
            op_name="swap_mint_on_destination",
            mint_url=dest_mint_url,
            retry_timeouts=False,
        )
    except Exception as e:
        if "11003" in str(e) or "outputs already signed" in str(e).lower():
            # Previous mint call signed outputs at the mint but failed before
            # bump_secret_derivation ran locally. Recover orphaned proofs and
            # advance the counter so the next request derives fresh secrets.
            logger.warning(
                "swap_to_trusted_mint: outputs already signed — recovering orphaned proofs",
                extra={
                    "mint_quote_id": mint_quote.quote,
                    "minted_amount": minted_amount,
                },
            )
            try:
                for keyset_id in dest_wallet.keysets:
                    await dest_wallet.restore_tokens_for_keyset(
                        keyset_id, to=1, batch=25
                    )
                await dest_wallet.load_proofs(reload=True)
                post_recovery_balance = dest_wallet.available_balance.amount
                balance_gained = post_recovery_balance - pre_mint_balance
                logger.info(
                    "swap_to_trusted_mint: recovery scan completed",
                    extra={
                        "pre_mint_balance": pre_mint_balance,
                        "post_recovery_balance": post_recovery_balance,
                        "balance_gained": balance_gained,
                        "expected": minted_amount,
                    },
                )
                if balance_gained < minted_amount:
                    # Recovery scan ran but did NOT restore the orphaned proofs
                    # (mint reports them as spent — they're stuck). Refuse to
                    # credit the API key balance for proofs we don't actually hold.
                    raise TokenConsumedError(
                        f"Swap recovery failed: mint signed outputs but proofs are "
                        f"unrecoverable (mint reports them spent). "
                        f"Expected {minted_amount}, recovered {balance_gained}. "
                        f"Local wallet DB ('.wallet/') state is corrupted — "
                        f"the counter for keyset is stuck at a bad index range."
                    )
            except TokenConsumedError:
                raise
            except Exception as recovery_err:
                logger.error(
                    "swap_to_trusted_mint: recovery failed",
                    extra={"error": str(recovery_err)},
                )
                raise TokenConsumedError(
                    f"Mint on primary failed and recovery unsuccessful: {e}"
                ) from e
        else:
            logger.error(
                "swap_to_trusted_mint: mint on primary failed after successful melt",
                extra={
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "minted_amount": minted_amount,
                    "mint_quote_id": mint_quote.quote,
                },
            )
            # Foreign proofs already melted (spent) — non-retryable.
            raise TokenConsumedError(
                "Mint on primary failed after successful melt"
            ) from e

    logger.info(
        "Cashu cross-mint swap completed",
        extra={
            "event": "cashu_swap_completed",
            "source_mint": token_obj.mint,
            "dest_mint": dest_mint_url,
            "original_amount": token_amount,
            "minted_amount": minted_amount,
            "unit": settings.primary_mint_unit,
        },
    )

    return int(minted_amount), settings.primary_mint_unit, dest_mint_url


async def swap_to_primary_mint(
    token_obj: Token, token_wallet: Wallet
) -> tuple[int, str, str]:
    """Backward-compatible alias for callers using the old function name."""
    return await swap_to_trusted_mint(token_obj, token_wallet)


async def credit_balance(
    cashu_token: str, key: db.ApiKey, session: db.AsyncSession
) -> int:
    async with wallet_operation_guard():
        return await _credit_balance_locked(cashu_token, key, session)


async def _credit_balance_locked(
    cashu_token: str, key: db.ApiKey, session: db.AsyncSession
) -> int:
    logger.info(
        "Starting Cashu balance credit",
        extra={
            "event": "cashu_credit_started",
            "key_hash": key.hashed_key[:8],
        },
    )

    try:
        destination_mint = key.refund_mint_url or settings.primary_mint
        amount, unit, mint_url = await recieve_token(
            cashu_token,
            destination_mint=destination_mint,
            destination_unit=key.refund_currency
            if isinstance(key.refund_currency, str)
            else None,
        )
        original_amount = amount
        original_unit = unit
        logger.info(
            "credit_balance: Token redeemed successfully",
            extra={"amount": amount, "unit": unit, "mint_url": mint_url},
        )

        if unit == "sat":
            amount = _sats_to_msats(amount)
            logger.info(
                "credit_balance: Converted to msat", extra={"amount_msat": amount}
            )

        # Guard against zero/negative redemptions (empty or dust tokens, or
        # swap-to-primary-mint amounts that net to <= 0 after fees). Raising here
        # — before the UPDATE/commit below — leaves any freshly-created, still
        # uncommitted ApiKey row to be rolled back when the request session
        # closes, instead of persisting an orphan key with balance 0.
        if amount <= 0:
            logger.error(
                "credit_balance: Redeemed amount is zero or negative; refusing to credit",
                extra={"amount": amount, "unit": unit, "mint_url": mint_url},
            )
            raise ValueError(
                f"Redeemed token amount must be positive, got {amount} msats"
            )

        logger.info(
            "credit_balance: Updating balance",
            extra={"old_balance": key.balance, "credit_amount": amount},
        )

        # The token is already redeemed (spent) here, so any crediting failure
        # is post-redemption and non-retryable — surface it as TokenConsumedError
        # (a key that vanished mid-flight, or an unexpected DB fault), never a
        # retryable/token-error taxonomy.
        try:
            # Atomic UPDATE to prevent race conditions during concurrent topups.
            updates: dict[str, object] = {
                "balance": db.ApiKey.balance + amount,
            }
            # Legacy keys may predate refund provenance. Pin them to the
            # destination used for this credit before exposing the balance.
            if key.refund_mint_url is None:
                updates["refund_mint_url"] = mint_url
            if key.refund_currency is None:
                updates["refund_currency"] = unit
            stmt = (
                update(db.ApiKey)
                .where(col(db.ApiKey.hashed_key) == key.hashed_key)
                .values(**updates)
            )
            result = await session.exec(stmt)  # type: ignore[call-overload]
            # If pruning removed this key after redemption, do not commit a no-op
            # balance update and pretend the top-up succeeded.
            if (getattr(result, "rowcount", 0) or 0) == 0:
                raise TokenConsumedError(
                    "Token redeemed but the API key disappeared before the "
                    "credit could be recorded"
                )
            await session.commit()
            await session.refresh(key)
            # refresh() starts a read transaction; release it before the
            # transaction-history write opens its own session below.
            await session.commit()
        except TokenConsumedError:
            raise
        except Exception as db_error:
            raise TokenConsumedError(
                "Token redeemed but crediting the balance failed"
            ) from db_error

        logger.info(
            "credit_balance: Balance updated successfully",
            extra={"new_balance": key.balance},
        )

        await store_cashu_transaction(
            token=cashu_token,
            amount=original_amount,
            unit=original_unit,
            mint_url=mint_url,
            typ="in",
            source="apikey",
            api_key_hashed_key=key.hashed_key,
        )
        logger.debug(
            "Cashu token successfully redeemed and stored",
            extra={"amount": amount, "unit": unit, "mint_url": mint_url},
        )
        return amount
    except Exception as e:
        logger.error(
            "credit_balance: Error during token redemption",
            extra={"error": str(e), "error_type": type(e).__name__},
        )
        raise


_wallets: dict[str, Wallet] = {}
_wallet_last_load: dict[str, float] = {}
_wallet_load_locks: dict[str, asyncio.Lock] = {}
# Minimum seconds between full mint info + proof reloads for the same
# wallet. Prevents redundant mint API calls when get_wallet(load=True)
# is called rapidly by multiple background tasks (balance fetch, payout,
# auto-topup all hitting get_wallet within the same cycle).
_WALLOAD_RELOAD_MIN_INTERVAL_SECONDS = 30


async def get_wallet(
    mint_url: str,
    unit: str = "sat",
    load: bool = True,
    retry_on_rate_limit: bool = True,
    force_reload: bool = False,
) -> Wallet:
    global _wallets, _wallet_last_load, _wallet_load_locks
    id = f"{mint_url}_{unit}"
    lock = _wallet_load_locks.setdefault(id, asyncio.Lock())
    async with lock:
        if id not in _wallets:
            _wallets[id] = await Wallet.with_db(mint_url, db=".wallet", unit=unit)

        if load:
            now = time.monotonic()
            last = _wallet_last_load.get(id)
            if (
                force_reload
                or last is None
                or now - last >= _WALLOAD_RELOAD_MIN_INTERVAL_SECONDS
            ):
                await run_mint_operation(
                    lambda: _wallets[id].load_mint(),
                    op_name="load_mint",
                    mint_url=mint_url,
                    retry_on_rate_limit=retry_on_rate_limit,
                )
                await run_mint_operation(
                    lambda: _wallets[id].load_proofs(reload=True),
                    op_name="load_proofs",
                    mint_url=mint_url,
                    retry_on_rate_limit=retry_on_rate_limit,
                )
                _wallet_last_load[id] = time.monotonic()
        return _wallets[id]


def get_proofs_per_mint_and_unit(
    wallet: Wallet, mint_url: str, unit: str, not_reserved: bool = False
) -> list[Proof]:
    valid_keyset_ids = [
        k.id
        for k in wallet.keysets.values()
        if k.mint_url == mint_url and k.unit.name == unit
    ]
    proofs = [p for p in wallet.proofs if p.id in valid_keyset_ids]
    if not_reserved:
        proofs = [p for p in proofs if not p.reserved]
    return proofs


async def slow_filter_spend_proofs(
    proofs: list[Proof],
    wallet: Wallet,
    *,
    retry_on_rate_limit: bool = True,
) -> list[Proof]:
    if not proofs:
        return []
    _proofs = []
    _spent_proofs = []
    # Keep proof-state checks in large batches. Mint quotas count HTTP requests,
    # so smaller batches make balance reads slower and more likely to hit 429s.
    batch_size = 1000
    for i in range(0, len(proofs), batch_size):
        pb = proofs[i : i + batch_size]
        proof_states = await run_mint_operation(
            lambda: wallet.check_proof_state(pb),
            op_name="check_proof_state",
            mint_url=str(wallet.url),
            retry_on_rate_limit=retry_on_rate_limit,
        )
        for proof, state in zip(pb, proof_states.states):
            if str(state.state) != "spent":
                _proofs.append(proof)
            else:
                _spent_proofs.append(proof)
    if _spent_proofs:
        await wallet.set_reserved_for_send(_spent_proofs, reserved=True)
    return _proofs


class BalanceDetail(TypedDict, total=False):
    mint_url: str
    unit: str
    wallet_balance: int
    user_balance: int
    owner_balance: int
    error: str
    error_code: str
    retry_after_seconds: float


_BALANCE_FETCH_RETRY_SECONDS = 60.0
_MINT_UNITS_CACHE_SECONDS = 300.0
_balance_fetch_failures: dict[tuple[str, str], tuple[float, str, str]] = {}
_balance_fetch_locks: dict[str, asyncio.Lock] = {}
_mint_supported_units: dict[str, tuple[float, list[str]]] = {}


async def _get_supported_mint_units(mint_url: str) -> list[str]:
    now = time.monotonic()
    cached = _mint_supported_units.get(mint_url)
    if cached is not None and now < cached[0]:
        return cached[1]

    wallet = await get_wallet(mint_url, settings.primary_mint_unit, load=False)
    keysets = await run_mint_operation(
        lambda: wallet._get_keysets(),
        op_name="get_mint_keysets",
        mint_url=mint_url,
        retry_on_rate_limit=False,
    )
    units: list[str] = []
    for keyset in keysets:
        if not keyset.active or keyset.unit is None:
            continue
        unit = keyset.unit if isinstance(keyset.unit, str) else keyset.unit.name
        if unit and unit not in units:
            units.append(unit)
    if not units:
        units = [settings.primary_mint_unit]
    elif settings.primary_mint_unit in units:
        units.remove(settings.primary_mint_unit)
        units.insert(0, settings.primary_mint_unit)

    _mint_supported_units[mint_url] = (
        time.monotonic() + _MINT_UNITS_CACHE_SECONDS,
        units,
    )
    return units


def _balance_error(
    mint_url: str,
    unit: str,
    error: str,
    *,
    error_code: str,
    retry_after_seconds: float | None = None,
) -> BalanceDetail:
    detail: BalanceDetail = {
        "mint_url": mint_url,
        "unit": unit,
        "wallet_balance": 0,
        "user_balance": 0,
        "owner_balance": 0,
        "error": error,
        "error_code": error_code,
    }
    if retry_after_seconds is not None:
        detail["retry_after_seconds"] = round(max(0.0, retry_after_seconds), 2)
    return detail


async def fetch_all_balances(
    units: list[str] | None = None,
) -> tuple[list[BalanceDetail], int, int, int]:
    """Fetch balances for all trusted mints without holding DB connections during I/O."""
    mint_urls = _mints_to_inspect()

    mint_units: dict[str, list[str]] = {}
    discovery_errors: list[BalanceDetail] = []
    if units is not None:
        mint_units = {mint_url: units for mint_url in mint_urls}
    else:
        for mint_url in mint_urls:
            try:
                mint_units[mint_url] = await _get_supported_mint_units(mint_url)
            except Exception as error:
                connection_failure = is_mint_connection_error(error)
                rate_limited = is_mint_rate_limited(error)
                error_code = (
                    "rate_limited"
                    if rate_limited
                    else "unreachable"
                    if connection_failure
                    else "mint_error"
                )
                if connection_failure:
                    MintRateGuard.get(mint_url).apply_cooldown(
                        _BALANCE_FETCH_RETRY_SECONDS, reason="unreachable"
                    )
                retry_delay = max(
                    _BALANCE_FETCH_RETRY_SECONDS,
                    mint_cooldown_remaining(mint_url),
                )
                discovery_errors.append(
                    _balance_error(
                        mint_url,
                        settings.primary_mint_unit,
                        str(error),
                        error_code=error_code,
                        retry_after_seconds=retry_delay,
                    )
                )
                mint_units[mint_url] = []
                if not connection_failure and not rate_limited:
                    logger.warning(
                        "Unable to discover mint units",
                        extra={
                            "mint_url": mint_url,
                            "error": str(error),
                            "error_type": type(error).__name__,
                        },
                    )

    # Read all liabilities in one short-lived transaction, then release the
    # connection before starting concurrent mint network requests.
    user_balances: dict[tuple[str, str], int] = {}
    liabilities_error: str | None = None
    query_units = list(
        dict.fromkeys(unit for mint_url in mint_urls for unit in mint_units[mint_url])
    )
    try:
        async with db.create_session() as session:
            user_balances = await db.balances_by_mint_and_unit(
                session, mint_urls, query_units
            )
    except Exception as error:
        logger.error("Error reading user balances", extra={"error": str(error)})
        liabilities_error = str(error)

    mint_check_limit = asyncio.Semaphore(settings.mint_operation_concurrency)

    async def fetch_balance(mint_url: str, unit: str) -> BalanceDetail:
        key = (mint_url, unit)
        lock = _balance_fetch_locks.setdefault(mint_url, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            failure = _balance_fetch_failures.get(key)
            if failure is not None and now < failure[0]:
                return _balance_error(
                    mint_url,
                    unit,
                    failure[1],
                    error_code=failure[2],
                    retry_after_seconds=failure[0] - now,
                )

            cooldown = mint_cooldown_remaining(mint_url)
            if cooldown > 0:
                error_code = mint_cooldown_reason(mint_url) or "cooldown"
                error = {
                    "rate_limited": "Mint is rate limited",
                    "unreachable": "Mint is unreachable",
                }.get(error_code, "Mint cooldown is active")
                _balance_fetch_failures[key] = (now + cooldown, error, error_code)
                return _balance_error(
                    mint_url,
                    unit,
                    error,
                    error_code=error_code,
                    retry_after_seconds=cooldown,
                )

            try:
                async with mint_check_limit:
                    wallet = await get_wallet(mint_url, unit, retry_on_rate_limit=False)
                    proofs = get_proofs_per_mint_and_unit(
                        wallet, mint_url, unit, not_reserved=True
                    )
                    proofs = await slow_filter_spend_proofs(proofs, wallet)
            except Exception as error:
                connection_failure = is_mint_connection_error(error)
                rate_limited = is_mint_rate_limited(error)
                error_code = (
                    "rate_limited"
                    if rate_limited
                    else "unreachable"
                    if connection_failure
                    else "mint_error"
                )
                if rate_limited:
                    MintRateGuard.get(mint_url).apply_rate_limit_cooldown(
                        _BALANCE_FETCH_RETRY_SECONDS
                    )
                elif connection_failure:
                    MintRateGuard.get(mint_url).apply_cooldown(
                        _BALANCE_FETCH_RETRY_SECONDS, reason=error_code
                    )
                retry_delay = max(
                    _BALANCE_FETCH_RETRY_SECONDS,
                    mint_cooldown_remaining(mint_url),
                )
                _balance_fetch_failures[key] = (
                    time.monotonic() + retry_delay,
                    str(error),
                    error_code,
                )
                logger.warning(
                    "Unable to refresh mint balance",
                    extra={
                        "mint_url": mint_url,
                        "unit": unit,
                        "error": str(error),
                        "connection_failure": connection_failure,
                        "rate_limited": rate_limited,
                        "mint_cooldown_applied": connection_failure or rate_limited,
                        "retry_seconds": round(retry_delay, 2),
                    },
                )
                return _balance_error(
                    mint_url,
                    unit,
                    str(error),
                    error_code=error_code,
                    retry_after_seconds=retry_delay,
                )

            _balance_fetch_failures.pop(key, None)
            user_balance = user_balances.get((mint_url, unit), 0)
            if unit == "sat":
                user_balance = _msats_to_sats(user_balance)
            proofs_balance = sum(proof.amount for proof in proofs)
            return {
                "mint_url": mint_url,
                "unit": unit,
                "wallet_balance": proofs_balance,
                "user_balance": user_balance,
                "owner_balance": proofs_balance - user_balance,
            }

    tasks = [
        fetch_balance(mint_url, unit)
        for mint_url in mint_urls
        for unit in mint_units[mint_url]
    ]
    balance_details = discovery_errors + list(await asyncio.gather(*tasks))

    total_wallet_balance_sats = 0
    total_user_balance_sats = 0
    for detail in balance_details:
        if detail.get("error"):
            continue
        unit = detail["unit"]
        total_wallet_balance_sats += (
            detail["wallet_balance"]
            if unit == "sat"
            else _msats_to_sats(detail["wallet_balance"])
        )
        if liabilities_error is None:
            total_user_balance_sats += (
                detail["user_balance"]
                if unit == "sat"
                else _msats_to_sats(detail["user_balance"])
            )

    if liabilities_error is None:
        owner_balance = total_wallet_balance_sats - total_user_balance_sats
    else:
        owner_balance = 0
        for detail in balance_details:
            detail["user_balance"] = 0
            detail["owner_balance"] = 0
            detail.setdefault("error", liabilities_error)

    return (
        balance_details,
        total_wallet_balance_sats,
        total_user_balance_sats,
        owner_balance,
    )


async def _payout_mint_and_unit(mint_url: str, unit: str) -> None:
    """Send only conservatively proven owner funds for one wallet."""
    try:
        # Runs under wallet_operation_guard; a cached wallet may carry a proof
        # snapshot up to 30s stale from another process's reservation, so the
        # cross-process lock is only safe with a fresh reload.
        wallet = await get_wallet(mint_url, unit, force_reload=True)
        proofs = get_proofs_per_mint_and_unit(wallet, mint_url, unit, not_reserved=True)
        proofs = await slow_filter_spend_proofs(proofs, wallet)
        await asyncio.sleep(5)
    except Exception as e:
        logger.error(
            f"Error sending payout: {type(e).__name__}",
            extra={"error": str(e), "mint_url": mint_url, "unit": unit},
        )
        return

    # Fetch liability after the proofs snapshot and settle delay while the
    # wallet operation guard excludes concurrent proof mutation and crediting.
    try:
        async with db.create_session() as session:
            # ApiKey stores a refund preference, not funding provenance. Until
            # liabilities have a durable per-credit ledger, subtract the total
            # liability from every wallet rather than risk calling customer
            # funds owner profit on the wrong mint.
            user_balance = await db.total_user_liability(session)
    except Exception as e:
        logger.error(
            f"Error in periodic payout cycle: {type(e).__name__}",
            extra={"error": str(e), "mint_url": mint_url, "unit": unit},
        )
        return

    try:
        if unit == "sat":
            user_balance = _msats_to_sats(user_balance)
        proofs_balance = sum(proof.amount for proof in proofs)
        available_balance = proofs_balance - user_balance
        min_amount = (
            settings.min_payout_sat
            if unit == "sat"
            else _sats_to_msats(settings.min_payout_sat)
        )
        if available_balance > min_amount:
            amount_received = await raw_send_to_lnurl(
                wallet,
                proofs,
                settings.receive_ln_address,
                unit,
                amount=available_balance,
            )
            logger.info(
                "Payout sent successfully",
                extra={
                    "mint_url": mint_url,
                    "unit": unit,
                    "balance": available_balance,
                    "amount_received": amount_received,
                },
            )
    except Exception as e:
        logger.error(
            f"Error sending payout: {type(e).__name__}",
            extra={"error": str(e), "mint_url": mint_url, "unit": unit},
        )


async def periodic_payout() -> None:
    while True:
        await asyncio.sleep(settings.payout_interval_seconds)
        try:
            if not settings.receive_ln_address:
                continue

            for mint_url in _mints_to_inspect():
                for unit in ["sat", "msat"]:
                    # Proof mutation, liability observation, and sending are one
                    # cross-process critical section. Credits take the same lock.
                    async with wallet_operation_guard():
                        await _payout_mint_and_unit(mint_url, unit)
        except Exception as e:
            logger.error(
                f"Error in periodic payout cycle: {type(e).__name__}",
                extra={"error": str(e)},
            )


async def _set_refund_sweep_state(
    refund_id: str,
    *,
    predicates: tuple[typing.Any, ...] = (),
    **values: object,
) -> int:
    async with db.create_session() as session:
        result = await session.exec(  # type: ignore[call-overload]
            update(db.CashuTransaction)
            .where(col(db.CashuTransaction.id) == refund_id, *predicates)
            .values(**values)
        )
        await session.commit()
        return int(result.rowcount or 0)


async def _refund_sweep_once(cutoff: int) -> None:
    claim_cutoff = int(time.time()) - settings.refund_sweep_claim_timeout_seconds
    claim_available = col(db.CashuTransaction.sweep_started_at).is_(None) | (
        col(db.CashuTransaction.sweep_started_at) < claim_cutoff
    )
    async with db.create_session() as session:
        stmt = select(db.CashuTransaction).where(
            db.CashuTransaction.type == "out",
            db.CashuTransaction.collected == False,  # noqa: E712
            db.CashuTransaction.swept == False,  # noqa: E712
            # PPQ rows describe a Lightning spend or claim lock, not a
            # refundable Cashu token. Preserve legacy rows without a source.
            col(db.CashuTransaction.source).is_(None)
            | col(db.CashuTransaction.source).notin_(
                ["ppq_auto_topup", "ppq_auto_topup_claim"]
            ),
            db.CashuTransaction.created_at < cutoff,
            claim_available,
        )
        results = await session.exec(stmt)
        refunds = results.all()

    for refund in refunds:
        reclaimed_stale_claim = refund.sweep_started_at is not None
        claim_started_at = int(time.time())
        claimed = await _set_refund_sweep_state(
            refund.id,
            predicates=(
                col(db.CashuTransaction.swept) == False,  # noqa: E712
                col(db.CashuTransaction.collected) == False,  # noqa: E712
                claim_available,
            ),
            sweep_started_at=claim_started_at,
        )
        if claimed != 1:
            continue

        claim_owned = col(db.CashuTransaction.sweep_started_at) == claim_started_at
        redeemed = False
        try:
            async with wallet_operation_guard():
                await recieve_token(refund.token)
            redeemed = True
            finalized = await _set_refund_sweep_state(
                refund.id,
                predicates=(claim_owned,),
                swept=True,
                sweep_started_at=None,
            )
            if finalized == 1:
                logger.info(
                    "Swept uncollected refund",
                    extra={
                        "id": refund.id,
                        "amount": refund.amount,
                        "unit": refund.unit,
                    },
                )
            else:
                logger.critical(
                    "Refund token swept after claim ownership changed; manual reconciliation required",
                    extra={"id": refund.id},
                )
        except BaseException as e:
            if redeemed or isinstance(e, TokenConsumedError):
                # The token was spent, or the redemption outcome is known to be
                # post-spend. Retain the claim so a stale retry classifies
                # "already spent" as swept, never as a client collection.
                logger.critical(
                    "Refund token spent but sweep checkpoint was not completed; manual reconciliation required",
                    extra={"id": refund.id},
                    exc_info=isinstance(e, Exception),
                )
                if not isinstance(e, Exception):
                    raise
                continue

            error_msg = str(e).lower()
            if isinstance(e, Exception) and "already spent" in error_msg:
                if reclaimed_stale_claim:
                    # A prior worker may have redeemed the token and crashed
                    # before finalizing. Treat the ambiguous stale claim as a
                    # completed sweep rather than misreporting client collection.
                    updated = await _set_refund_sweep_state(
                        refund.id,
                        predicates=(claim_owned,),
                        swept=True,
                        sweep_started_at=None,
                    )
                else:
                    updated = await _set_refund_sweep_state(
                        refund.id,
                        predicates=(claim_owned,),
                        collected=True,
                        swept=False,
                        sweep_started_at=None,
                    )
                if updated == 1:
                    logger.info(
                        "Refund token was already spent",
                        extra={
                            "id": refund.id,
                            "reclaimed_stale_claim": reclaimed_stale_claim,
                        },
                    )
                else:
                    logger.warning(
                        "Refund claim ownership changed before spent-token checkpoint",
                        extra={"id": refund.id},
                    )
            else:
                # Once redemption starts, an exception cannot prove the token
                # was not spent (for example, a melt may land before the
                # response is lost). Retain the claim so a stale retry treats
                # an "already spent" result as a completed sweep.
                logger.critical(
                    "Refund token redemption outcome is unknown; retaining sweep claim for reconciliation",
                    extra={"id": refund.id, "error": str(e)},
                    exc_info=isinstance(e, Exception),
                )
                if not isinstance(e, Exception):
                    raise


async def refund_sweep_once() -> None:
    """Sweep eligible uncollected refund tokens once."""
    cutoff = int(time.time()) - settings.refund_sweep_ttl_seconds
    await _refund_sweep_once(cutoff)


async def periodic_refund_sweep() -> None:
    while True:
        await asyncio.sleep(60 * 60)  # every hour
        try:
            await refund_sweep_once()
        except Exception as e:
            logger.error(
                "Error in periodic refund sweep",
                extra={"error": str(e), "error_type": type(e).__name__},
            )


class _RoutstrFeePayoutAlreadyClaimed(Exception):
    """Another worker claimed the fee balance before melt dispatch."""


async def periodic_routstr_fee_payout() -> None:
    from .auth import (
        ROUTSTR_FEE_DEFAULT_PAYOUT,
        ROUTSTR_FEE_PAYOUT_INTERVAL_SECONDS,
        ROUTSTR_LN_ADDRESS,
    )

    if not ROUTSTR_LN_ADDRESS:
        logger.info("ROUTSTR_LN_ADDRESS not set, skipping fee payout")
        return
    while True:
        await asyncio.sleep(ROUTSTR_FEE_PAYOUT_INTERVAL_SECONDS)
        try:
            async with db.create_session() as session:
                fee = await db.get_routstr_fee(session)
                payout_in_progress_msats = fee.payout_in_progress_msats
                accumulated_sats = _msats_to_sats(fee.accumulated_msats)

            if payout_in_progress_msats:
                # Dispatch holds the same guard from before checkpoint creation
                # through melt completion. Re-read after taking it so a second
                # worker cannot reconcile the quote between checkpoint and melt.
                async with wallet_operation_guard():
                    async with db.create_session() as session:
                        fee = await db.get_routstr_fee(session)
                        payout_in_progress_msats = fee.payout_in_progress_msats
                        payout_started_at = fee.payout_started_at
                        payout_quote_id = getattr(fee, "payout_quote_id", None)
                        payout_mint_url = getattr(fee, "payout_mint_url", None)
                        payout_unit = getattr(fee, "payout_unit", None)

                    if not payout_in_progress_msats:
                        continue
                    if not (payout_quote_id and payout_mint_url and payout_unit):
                        logger.critical(
                            "Routstr fee payout lacks reconciliation metadata",
                            extra={
                                "payout_in_progress_msats": payout_in_progress_msats,
                                "payout_started_at": payout_started_at,
                            },
                        )
                        continue

                    quote_state = await _check_bolt11_payment_status_locked(
                        payout_mint_url, payout_unit, payout_quote_id
                    )
                    if quote_state == "paid":
                        async with db.create_session() as session:
                            completed = await db.complete_routstr_fee_payout(
                                session,
                                payout_in_progress_msats,
                                payout_quote_id,
                                payout_mint_url,
                                payout_unit,
                            )
                        if completed:
                            logger.info(
                                "Routstr fee payout reconciled as paid",
                                extra={"payout_quote_id": payout_quote_id},
                            )
                    elif quote_state == "unpaid":
                        async with db.create_session() as session:
                            restored = await db.restore_routstr_fee_payout(
                                session,
                                payout_in_progress_msats,
                                payout_quote_id,
                                payout_mint_url,
                                payout_unit,
                            )
                        if restored:
                            logger.warning(
                                "Routstr fee payout reconciled as unpaid and restored for retry",
                                extra={"payout_quote_id": payout_quote_id},
                            )
                    else:
                        logger.warning(
                            "Routstr fee payout is still awaiting reconciliation",
                            extra={
                                "payout_quote_id": payout_quote_id,
                                "quote_state": quote_state,
                            },
                        )
                continue

            if accumulated_sats < ROUTSTR_FEE_DEFAULT_PAYOUT:
                continue
            paid_msats = _sats_to_msats(accumulated_sats)

            # Serialize proof refresh, quote creation, checkpointing, sending,
            # and finalization with every other wallet mutation across workers.
            async with wallet_operation_guard():
                wallet = await get_wallet(
                    settings.primary_mint, "sat", force_reload=True
                )
                proofs = get_proofs_per_mint_and_unit(
                    wallet, settings.primary_mint, "sat", not_reserved=True
                )

                attempt_quote_id: str | None = None

                async def checkpoint_quote(quote_id: str) -> None:
                    nonlocal attempt_quote_id
                    async with db.create_session() as session:
                        checkpointed = await db.reset_routstr_fee(
                            session,
                            paid_msats,
                            quote_id,
                            settings.primary_mint,
                            "sat",
                        )
                    if not checkpointed:
                        raise _RoutstrFeePayoutAlreadyClaimed
                    attempt_quote_id = quote_id

                try:
                    amount_received = await raw_send_to_lnurl(
                        wallet,
                        proofs,
                        ROUTSTR_LN_ADDRESS,
                        "sat",
                        amount=accumulated_sats,
                        on_melt_quote=checkpoint_quote,
                    )
                except _RoutstrFeePayoutAlreadyClaimed:
                    logger.warning("Routstr fee payout was already claimed")
                    continue
                except BaseException as e:
                    logger.critical(
                        "Routstr fee payout outcome is unknown; awaiting quote reconciliation",
                        extra={"payout_in_progress_msats": paid_msats},
                        exc_info=isinstance(e, Exception),
                    )
                    if not isinstance(e, Exception):
                        raise
                    continue

                assert attempt_quote_id is not None
                try:
                    async with db.create_session() as session:
                        payout_completed = await db.complete_routstr_fee_payout(
                            session,
                            paid_msats,
                            attempt_quote_id,
                            settings.primary_mint,
                            "sat",
                        )
                except BaseException as e:
                    logger.critical(
                        "Routstr fee payout sent but checkpoint was not completed; awaiting quote reconciliation",
                        extra={"payout_in_progress_msats": paid_msats},
                        exc_info=isinstance(e, Exception),
                    )
                    if not isinstance(e, Exception):
                        raise
                    continue
                if not payout_completed:
                    logger.critical(
                        "Routstr fee payout sent but checkpoint was not completed; awaiting quote reconciliation",
                        extra={"payout_in_progress_msats": paid_msats},
                    )
                    continue

                logger.info(
                    "Routstr fee payout sent",
                    extra={
                        "accumulated_sats": accumulated_sats,
                        "amount_received": amount_received,
                    },
                )
        except Exception as e:
            logger.error(
                f"Error in Routstr fee payout: {type(e).__name__}",
                extra={"error": str(e)},
            )


async def send_to_lnurl(amount: int, unit: str, mint: str, address: str) -> int:
    async with wallet_operation_guard():
        mint = await find_trusted_mint_with_funds(amount, unit, mint, force_reload=True)
        wallet = await get_wallet(mint, unit)
        available = get_proofs_per_mint_and_unit(wallet, mint, unit, not_reserved=True)
        proofs, _ = await wallet.select_to_send(available, amount, set_reserved=True)
        return await raw_send_to_lnurl(wallet, proofs, address, unit)


# class Payment:
#     """
#     Stores all cashu payment related data
#     """

#     def __init__(self, token: str) -> None:
#         self.initial_token = token
#         amount, unit, mint_url = self.parse_token(token)
#         self.amount = amount
#         self.unit = unit
#         self.mint_url = mint_url

#         self.claimed_proofs = redeem_to_proofs(token)

#     def parse_token(self, token: str) -> tuple[int, CurrencyUnit, str]:
#         raise NotImplementedError

#     def refund_full(self) -> None:
#         raise NotImplementedError

#     def refund_partial(self, amount: int) -> None:
#         raise NotImplementedError
