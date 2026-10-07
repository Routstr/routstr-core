"""Cross-mint swaps for tokens from mints the operator did not configure.

The original swap path was removed in cc55868e because it contacted the
sender's mint while holding the process-wide wallet lock. This version keeps
three things apart that were mixed before:

* **Trust and destination checks** run offline first. The mint URL inside the
  token is unauthenticated input and gets the same SSRF treatment as any other
  client-supplied URL.
* **Foreign-mint I/O** runs outside ``wallet_operation_guard`` under its own
  budget: one attempt, a short deadline, a process-wide concurrency cap and a
  per-mint file lock. A dead or hostile mint stalls its own swap and, while
  its melts hold global slots, other foreign-mint swaps (refused with a
  retryable busy error); trusted-mint top-ups are never affected.
* **Wallet mutation** (minting on a trusted mint, crediting a key) runs under
  the guard as before, but only after the Lightning leg has settled.

Every money movement is journaled in ``cashu_swaps`` before it is dispatched,
so a crash or timeout leaves a row the reconciler can finish or fail.
"""

import asyncio
import fcntl
import hashlib
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Awaitable, Callable

from cashu.core.base import MeltQuote, MintQuote, Proof, Token, TokenV3, TokenV3Token
from cashu.wallet.helpers import deserialize_token_from_string
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select, update

from . import wallet as _wallet_module
from .core import db, get_logger
from .core.db import SWAP_OPEN_STATUSES, ApiKey, AsyncSession, CashuSwap, Refund
from .core.settings import settings
from .mint import (
    MINT_TRANSPORT_COOLDOWN_SECONDS,
    MINT_TRANSPORT_EXCEPTIONS,
    MintRateGuard,
    mint_cooldown_remaining,
    run_mint_operation,
)
from .net_guard import BlockedDestinationError, assert_public_https_origin
from .wallet import (
    Bolt11PaymentAmbiguous,
    Bolt11PaymentNotAttempted,
    Bolt11PaymentPlan,
    ForeignMintBusyError,
    ForeignMintSwapError,
    ForeignMintUnavailableError,
    SwapPendingError,
    TokenConsumedError,
    Wallet,
    _apply_credit_locked,
    _check_bolt11_payment_status_locked,
    _execute_bolt11_payment,
    _wallet_operation_depth,
    find_trusted_mint_with_funds,
    get_proofs_per_mint_and_unit,
    get_supported_mint_units,
    get_wallet,
    preferred_trusted_mint,
    resolve_trusted_source_mint,
    select_melt_inputs,
    wallet_operation_guard,
)

logger = get_logger(__name__)

RECONCILE_BATCH_LIMIT = 100
_UNITS = ("sat", "msat")

_MINT_ERROR_CODE_RE = re.compile(r"\(Code: (\d+)\)")
_FOREIGN_FAILURE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    asyncio.TimeoutError,
    *MINT_TRANSPORT_EXCEPTIONS,
)


async def refund_destination_mint(key: ApiKey, session: AsyncSession) -> str | None:
    """Return the foreign mint that originally funded this key, if any."""
    mint = key.refund_mint_url
    if not mint or resolve_trusted_source_mint(mint):
        return None
    credited_swap = await session.exec(
        select(CashuSwap.id)
        .where(CashuSwap.direction == "in")
        .where(CashuSwap.status == "credited")
        .where(CashuSwap.api_key_hashed_key == key.hashed_key)
        .where(CashuSwap.source_mint == mint)
        .limit(1)
    )
    return mint if credited_swap.first() is not None else None


# --- foreign-mint budget ---------------------------------------------------


_foreign_slots: asyncio.Semaphore | None = None
_foreign_slots_capacity = 0


def _slots() -> asyncio.Semaphore:
    global _foreign_slots, _foreign_slots_capacity
    capacity = settings.foreign_mint_max_concurrency
    if _foreign_slots is None or _foreign_slots_capacity != capacity:
        _foreign_slots = asyncio.Semaphore(capacity)
        _foreign_slots_capacity = capacity
    return _foreign_slots


def _assert_outside_wallet_guard(what: str) -> None:
    # A programming error, not a runtime condition: this is the exact shape of
    # the DoS that got the feature removed.
    if _wallet_operation_depth.get():
        raise RuntimeError(f"{what} must not run under wallet_operation_guard")


@asynccontextmanager
async def foreign_mint_lock(mint_url: str) -> AsyncGenerator[None, None]:
    """Serialize all work against one foreign mint across worker processes.

    The foreign wallet's secret-derivation counter lives in the shared wallet
    database, so two processes minting change on the same foreign mint would
    collide. Bounded wait: a slot that does not free up within the foreign
    budget is treated like an unreachable mint, not queued behind.
    """
    _assert_outside_wallet_guard("foreign_mint_lock")
    digest = hashlib.sha256(mint_url.strip().lower().encode()).hexdigest()[:16]
    # Resolved at call time so tests that relocate the wallet lock move this too.
    path = (
        _wallet_module._WALLET_OPERATION_LOCK.parent / f".routstr-foreign-{digest}.lock"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + settings.foreign_mint_operation_timeout_seconds
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ForeignMintBusyError(
                        "Another swap against this mint is still in progress"
                    ) from None
                await asyncio.sleep(0.05)
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


async def run_foreign_mint_operation(
    factory: Callable[[], Awaitable[Any]],
    *,
    mint_url: str,
    op_name: str,
    timeout: float | None = None,
    cooldown_on_timeout: bool = True,
) -> Any:
    """One bounded attempt against a mint the operator did not configure.

    No retries and no queueing: if every slot is busy or the mint is cooling
    down, fail now. A transport failure or timeout puts the mint on the normal
    transport cooldown so a flood naming the same dead mint is refused offline.
    """
    _assert_outside_wallet_guard(op_name)
    if mint_cooldown_remaining(mint_url) > 0:
        raise ForeignMintUnavailableError("Issuing mint is cooling down")
    slots = _slots()
    if slots.locked():
        raise ForeignMintBusyError("Foreign-mint budget exhausted; retry later")
    await slots.acquire()
    try:
        return await asyncio.wait_for(
            factory(),
            timeout=timeout or settings.foreign_mint_operation_timeout_seconds,
        )
    except _FOREIGN_FAILURE_EXCEPTIONS as error:
        if cooldown_on_timeout or not isinstance(error, asyncio.TimeoutError):
            MintRateGuard.get(mint_url).apply_cooldown(
                MINT_TRANSPORT_COOLDOWN_SECONDS, reason="transport"
            )
        logger.warning(
            "Foreign mint operation failed",
            extra={
                "event": "foreign_mint_operation_failed",
                "op_name": op_name,
                "mint_url": mint_url,
                "error_type": type(error).__name__,
            },
        )
        raise ForeignMintUnavailableError(
            f"Issuing mint did not answer {op_name} in time"
        ) from error
    finally:
        slots.release()


# --- amounts ----------------------------------------------------------------


def _convert(amount: int, from_unit: str, to_unit: str) -> int:
    msats = amount * 1000 if from_unit == "sat" else amount
    return msats // 1000 if to_unit == "sat" else msats


def _melt_definitively_failed(error: BaseException) -> bool:
    """The mint authoritatively rejected the Lightning payment; proofs are unspent."""
    message = str(error).strip()
    return message.lower() == "could not pay invoice." or "(Code: 20004)" in message


def _melt_rejected_inputs(error: BaseException) -> bool:
    """The mint refused the melt before paying because the inputs fell short.

    11005 is the registered "Transaction is not balanced" code (cdk). 11000 is
    nutshell's generic TransactionError and only counts alongside the
    "not enough inputs" detail text.
    """
    message = str(error)
    match = _MINT_ERROR_CODE_RE.search(message)
    code = match.group(1) if match else None
    shortfall_text = "not enough inputs" in message.lower()
    return code == "11005" or (code in (None, "11000") and shortfall_text)


def _state_name(response: object) -> str:
    raw = getattr(response, "state", None)
    if raw is None:
        return "paid" if getattr(response, "paid", None) is True else ""
    return str(raw).lower().rsplit(".", 1)[-1]


@dataclass(frozen=True)
class SwapInResult:
    credited_msats: int
    change_token: str | None = None
    change_amount: int = 0
    change_unit: str | None = None


async def _melt_change_token(
    wallet: Wallet, before: set[str], *, mint_url: str, unit: str
) -> tuple[str | None, int]:
    change = [proof for proof in wallet.proofs if proof.secret not in before]
    if not change:
        return None, 0
    await wallet.set_reserved_for_send(change, reserved=True)
    token = TokenV3(
        token=[TokenV3Token(mint=mint_url, proofs=change)], _unit=unit
    ).serialize()
    return token, sum(proof.amount for proof in change)


# --- journal ----------------------------------------------------------------


async def _save(swap: CashuSwap) -> None:
    async with db.create_session() as session:
        session.add(swap)
        await session.commit()


async def _update(swap: CashuSwap, **values: Any) -> None:
    values.setdefault("updated_at", int(time.time()))
    async with db.create_session() as session:
        await session.exec(  # type: ignore[call-overload]
            update(CashuSwap).where(col(CashuSwap.id) == swap.id).values(**values)
        )
        await session.commit()
    for name, value in values.items():
        setattr(swap, name, value)


async def _transition_status(
    swap: CashuSwap, expected: str, status: str, **values: Any
) -> bool:
    values.update(status=status, updated_at=int(time.time()))
    async with db.create_session() as session:
        result = await session.exec(  # type: ignore[call-overload]
            update(CashuSwap)
            .where(col(CashuSwap.id) == swap.id)
            .where(col(CashuSwap.status) == expected)
            .values(**values)
        )
        await session.commit()
    transitioned = (getattr(result, "rowcount", 0) or 0) == 1
    if transitioned:
        for name, value in values.items():
            setattr(swap, name, value)
    return transitioned


async def _load_swap(swap_id: str) -> CashuSwap | None:
    async with db.create_session() as session:
        return await session.get(CashuSwap, swap_id)


async def _prior_swap_for_token(token_hash: str) -> CashuSwap | None:
    async with db.create_session() as session:
        result = await session.exec(
            select(CashuSwap)
            .where(CashuSwap.token_hash == token_hash)
            .order_by(col(CashuSwap.created_at).desc())
        )
        return result.first()


def _raise_for_prior_swap(prior: CashuSwap) -> None:
    if prior.status in SWAP_OPEN_STATUSES:
        raise SwapPendingError("A swap for this token is already in progress")
    if prior.status == "failed":
        raise ForeignMintSwapError(
            "A prior swap for this token failed; the token was not spent"
        )
    raise ValueError("Cashu token already spent")


# --- inbound: foreign token -> trusted mint -> API key credit -------------


async def _load_foreign_proofs(wallet: Wallet, token_obj: Token) -> list[Proof]:
    await run_foreign_mint_operation(
        wallet.load_mint_keysets, mint_url=token_obj.mint, op_name="swap_load_keysets"
    )
    try:
        await wallet.activate_keyset()
    except Exception as error:
        raise ForeignMintSwapError("Issuing mint has no active keyset") from error
    proofs = token_obj.proofs
    try:
        await wallet._expand_short_keyset_ids(proofs)
    except (KeyError, ValueError) as error:
        raise ForeignMintSwapError(
            "Cashu token references an unknown or ambiguous keyset"
        ) from error
    try:
        wallet.verify_proofs_dleq(proofs)
    except Exception as error:
        raise ValueError("Invalid Cashu token: DLEQ proof failed") from error
    return proofs


async def _quote_pair(
    dest_wallet: Wallet,
    dest_mint: str,
    source_wallet: Wallet,
    source_mint: str,
    amount: int,
) -> tuple[MintQuote, MeltQuote]:
    mint_quote = await run_mint_operation(
        lambda: dest_wallet.request_mint(amount),
        op_name="swap_request_mint",
        mint_url=dest_mint,
        retry_timeouts=False,
    )
    melt_quote = await run_foreign_mint_operation(
        lambda: source_wallet.melt_quote(mint_quote.request),
        mint_url=source_mint,
        op_name="swap_melt_quote",
    )
    return mint_quote, melt_quote


def _trusted_swap_destination() -> str:
    try:
        return preferred_trusted_mint()
    except ValueError as error:
        raise ForeignMintSwapError(
            "No trusted destination mint is configured"
        ) from error


async def _trusted_mint_unit(
    mint_url: str,
    *,
    liability_unit: str | None,
    source_unit: str,
    bolt11_operation: str,
) -> str:
    supported = [
        unit
        for unit in await get_supported_mint_units(
            mint_url, bolt11_operation=bolt11_operation
        )
        if unit in _UNITS
    ]
    if liability_unit is not None:
        if liability_unit not in supported:
            raise ValueError(
                "Trusted mint does not support the API key liability unit: "
                f"{liability_unit}"
            )
        return liability_unit
    for candidate in (source_unit, "sat", "msat"):
        if candidate in supported:
            return candidate
    raise ForeignMintSwapError(
        "Trusted destination mint has no supported Bolt11 sat or msat unit"
    )


async def swap_in_and_credit(
    cashu_token: str, key: ApiKey, session: AsyncSession
) -> SwapInResult:
    """Melt a foreign-mint token into a trusted mint and credit ``key``.

    The first configured ``CASHU_MINTS`` entry is the deterministic destination;
    list order is the operator's priority order.

    Returns the credited msats and any unused fee reserve as a source-mint
    change token, which is also kept on the swap row. Raises before anything
    is spent for every
    refusal (``ForeignMintSwapError``, ``ForeignMintUnavailableError``,
    ``ValueError``) and ``SwapPendingError`` once the melt was dispatched but
    not confirmed.
    """
    token_obj = deserialize_token_from_string(cashu_token)
    source_mint = str(token_obj.mint)
    if resolve_trusted_source_mint(source_mint) is not None:
        raise ValueError("Token is from a trusted mint; redeem it directly")
    source_unit = str(token_obj.unit)
    if source_unit not in _UNITS:
        raise ForeignMintSwapError("Unsupported token unit for swap")
    try:
        await assert_public_https_origin(source_mint)
    except BlockedDestinationError as error:
        raise ForeignMintSwapError(str(error)) from error

    dest_mint = _trusted_swap_destination()
    dest_unit = await _trusted_mint_unit(
        dest_mint,
        liability_unit=key.refund_currency,
        source_unit=source_unit,
        bolt11_operation="mint",
    )
    token_hash = hashlib.sha256(cashu_token.encode()).hexdigest()
    prior = await _prior_swap_for_token(token_hash)
    if prior is not None:
        _raise_for_prior_swap(prior)

    source_amount = int(token_obj.amount)
    swap: CashuSwap
    async with foreign_mint_lock(source_mint):
        # The first check fails fast. This second check closes the in-process
        # race for requests that were already waiting on the per-mint lock;
        # the unique token hash below closes it across worker processes.
        prior = await _prior_swap_for_token(token_hash)
        if prior is not None:
            _raise_for_prior_swap(prior)

        source_wallet = await get_wallet(source_mint, source_unit, load=False)
        proofs = await _load_foreign_proofs(source_wallet, token_obj)
        input_fees = source_wallet.get_fees_for_proofs(proofs)
        dest_wallet = await get_wallet(dest_mint, dest_unit, load_proofs=False)

        # Round one quotes the whole token minus input fees; the melt quote
        # then tells us the real Lightning fee reserve. Round two, if needed,
        # re-quotes for what is left. No open-ended retry loop: a mint whose
        # second quote still does not fit is refused with nothing spent.
        gross = _convert(source_amount - input_fees, source_unit, dest_unit)
        if gross <= 0:
            raise ForeignMintSwapError(
                "Token value does not cover the mint's input fees"
            )
        mint_quote, melt_quote = await _quote_pair(
            dest_wallet, dest_mint, source_wallet, source_mint, gross
        )
        needed = melt_quote.amount + melt_quote.fee_reserve + input_fees
        if needed > source_amount:
            net = _convert(
                source_amount - input_fees - melt_quote.fee_reserve,
                source_unit,
                dest_unit,
            )
            if net <= 0:
                raise ForeignMintSwapError("Token value does not cover swap fees")
            mint_quote, melt_quote = await _quote_pair(
                dest_wallet, dest_mint, source_wallet, source_mint, net
            )
            needed = melt_quote.amount + melt_quote.fee_reserve + input_fees
            if needed > source_amount:
                raise ForeignMintSwapError("Token value does not cover swap fees")
            minted_amount = net
        else:
            minted_amount = gross

        swap = CashuSwap(
            direction="in",
            status="melting",
            api_key_hashed_key=key.hashed_key,
            token_hash=token_hash,
            token=cashu_token,
            source_mint=source_mint,
            source_unit=source_unit,
            source_amount=source_amount,
            destination_mint=dest_mint,
            destination_unit=dest_unit,
            destination_amount=minted_amount,
            fee_reserve=int(melt_quote.fee_reserve),
            input_fees=input_fees,
            mint_quote_id=mint_quote.quote,
            melt_quote_id=melt_quote.quote,
        )
        try:
            await _save(swap)
        except IntegrityError:
            prior = await _prior_swap_for_token(token_hash)
            if prior is not None:
                _raise_for_prior_swap(prior)
            raise
        logger.info(
            "Cross-mint swap dispatching melt",
            extra={
                "event": "cashu_swap_melting",
                "swap_id": swap.id,
                "source_mint": source_mint,
                "destination_mint": dest_mint,
                "source_amount": source_amount,
                "minted_amount": minted_amount,
                "fee_reserve": melt_quote.fee_reserve,
                "input_fees": input_fees,
            },
        )

        proofs_before_melt = {proof.secret for proof in source_wallet.proofs}
        try:
            response = await run_foreign_mint_operation(
                lambda: source_wallet.melt(
                    proofs=proofs,
                    invoice=mint_quote.request,
                    fee_reserve_sat=melt_quote.fee_reserve,
                    quote_id=melt_quote.quote,
                ),
                mint_url=source_mint,
                op_name="swap_melt",
                timeout=settings.foreign_mint_melt_timeout_seconds,
                cooldown_on_timeout=False,
            )
        except ForeignMintBusyError:
            # Refused before dispatch: the melt never reached the mint.
            await _update(
                swap, status="failed", token_hash=None, error="foreign-mint budget busy"
            )
            raise
        except ForeignMintUnavailableError as error:
            # Dispatched, outcome unknown: the Lightning payment may still land.
            await _update(swap, status="ambiguous", error=str(error))
            raise SwapPendingError("Source melt outcome unknown") from error
        except Exception as error:
            if _melt_definitively_failed(error) or _melt_rejected_inputs(error):
                await _update(swap, status="failed", token_hash=None, error=str(error))
                raise ForeignMintSwapError(
                    "Issuing mint refused the Lightning payment"
                ) from error
            if "already spent" in str(error).lower():
                await _update(swap, status="failed", error=str(error))
                raise ValueError("Cashu token already spent") from error
            await _update(swap, status="ambiguous", error=str(error))
            raise SwapPendingError("Source melt outcome unknown") from error

        state = _state_name(response)
        if state == "unpaid":
            await _update(
                swap,
                status="failed",
                token_hash=None,
                error="melt reported unpaid",
            )
            raise ForeignMintSwapError("Issuing mint did not pay the swap invoice")
        if state != "paid":
            await _update(swap, status="ambiguous", error=f"melt state {state!r}")
            raise SwapPendingError("Source melt is still pending")
        change_token, change_amount = await _melt_change_token(
            source_wallet,
            proofs_before_melt,
            mint_url=source_mint,
            unit=source_unit,
        )
        await _update(swap, status="melted", change_token=change_token, error=None)
        if change_token:
            logger.info(
                "Cross-mint swap stored melt change",
                extra={
                    "event": "cashu_swap_change_stored",
                    "swap_id": swap.id,
                    "source_mint": source_mint,
                    "change_amount": change_amount,
                    "unit": source_unit,
                },
            )

    credited_msats = await _finish_swap_in(swap, key=key, session=session)
    return SwapInResult(
        credited_msats=credited_msats,
        change_token=change_token,
        change_amount=change_amount,
        change_unit=source_unit if change_token else None,
    )


async def _mint_with_recovery(
    wallet: Wallet,
    amount: int,
    quote_id: str,
    *,
    mint_url: str,
    foreign: bool,
) -> list[Proof]:
    """Mint for a paid quote; recover proofs the mint already signed once.

    An earlier attempt may have signed outputs at the mint but died before the
    local derivation counter advanced. Restoring the keyset recovers those
    proofs instead of crediting money the wallet does not hold.
    """
    await wallet.load_proofs(reload=True)

    def proofs_for_quote() -> list[Proof]:
        proofs = [
            proof
            for proof in wallet.proofs
            if getattr(proof, "mint_id", None) == quote_id
        ]
        return proofs if sum(proof.amount for proof in proofs) == amount else []

    # A prior attempt may have completed remotely and in the wallet DB before
    # the swap journal commit. Cashu records the quote id on every minted proof,
    # which is the idempotency key we need to resume without minting or crediting
    # a different set of proofs.
    existing = proofs_for_quote()
    if existing:
        return existing
    before = wallet.available_balance.amount

    async def do_mint() -> list[Proof]:
        return await wallet.mint(amount, quote_id=quote_id)

    try:
        if foreign:
            return await run_foreign_mint_operation(
                do_mint, mint_url=mint_url, op_name="swap_mint_foreign"
            )
        return await run_mint_operation(
            do_mint,
            op_name="swap_mint_on_destination",
            mint_url=mint_url,
            retry_timeouts=False,
        )
    except Exception as error:
        text = str(error).lower()
        if "11003" not in text and "outputs already signed" not in text:
            raise
        logger.warning(
            "Swap mint outputs already signed; recovering orphaned proofs",
            extra={"mint_url": mint_url, "quote_id": quote_id, "amount": amount},
        )
        for keyset_id in list(wallet.keysets):
            await wallet.restore_tokens_for_keyset(keyset_id, to=1, batch=25)
        await wallet.load_proofs(reload=True)
        recovered_for_quote = proofs_for_quote()
        if recovered_for_quote:
            return recovered_for_quote
        gained = wallet.available_balance.amount - before
        if gained < amount:
            raise TokenConsumedError(
                f"Swap recovery restored {gained} of {amount}; manual reconciliation required"
            ) from error
        recovered = [p for p in wallet.proofs if not p.reserved]
        try:
            # offline: never ask the mint to split here, the budget is spent.
            picked, _ = await wallet.select_to_send(
                recovered, amount, set_reserved=False, offline=True
            )
        except Exception as selection_error:
            raise TokenConsumedError(
                "Swap recovery restored proofs but none match the swapped amount; "
                "manual reconciliation required"
            ) from selection_error
        return picked


async def _finish_swap_in(
    swap: CashuSwap,
    *,
    key: ApiKey | None = None,
    session: AsyncSession | None = None,
) -> int:
    """Mint on the trusted destination and credit the key, under the guard."""
    started_melted = swap.status == "melted"

    def credited_msats() -> int:
        return swap.destination_amount * (1000 if swap.destination_unit == "sat" else 1)

    async with wallet_operation_guard():
        if swap.status == "melted":
            dest_wallet = await get_wallet(swap.destination_mint, swap.destination_unit)
            try:
                await _mint_with_recovery(
                    dest_wallet,
                    swap.destination_amount,
                    str(swap.mint_quote_id),
                    mint_url=swap.destination_mint,
                    foreign=False,
                )
            except TokenConsumedError as error:
                await _update(swap, error=str(error))
                raise
            except Exception as error:
                # Invoice is paid; the quote stays mintable. Leave the row for
                # the reconciler rather than losing track of settled money.
                await _update(swap, error=str(error))
                logger.error(
                    "Swap mint on destination failed after a paid melt",
                    extra={"swap_id": swap.id, "error": str(error)},
                )
                raise SwapPendingError(
                    "Destination mint failed; retrying later"
                ) from error
            transitioned = await _transition_status(
                swap, "melted", "minted", error=None
            )
            if not transitioned:
                stored = await _load_swap(swap.id)
                if stored is not None and stored.status == "credited":
                    return credited_msats()
                if stored is None or stored.status != "minted":
                    status = stored.status if stored is not None else "missing"
                    raise SwapPendingError(f"Swap is {status}")
                swap.status = "minted"
                swap.error = stored.error

        if swap.status != "minted":
            raise SwapPendingError(f"Swap is {swap.status}")

        try:
            if session is None or key is None:
                async with db.create_session() as own_session:
                    own_key = await own_session.get(ApiKey, swap.api_key_hashed_key)
                    if own_key is None:
                        await _update(swap, status="failed", error="api key missing")
                        logger.critical(
                            "Swapped funds have no API key to credit",
                            extra={
                                "swap_id": swap.id,
                                "amount": swap.destination_amount,
                            },
                        )
                        raise TokenConsumedError("API key vanished before swap credit")
                    credited = await _apply_credit_locked(
                        own_key,
                        own_session,
                        amount=swap.destination_amount,
                        unit=swap.destination_unit,
                        mint_url=swap.destination_mint,
                        token=str(swap.token),
                        refund_mint_url=swap.source_mint,
                        swap_id=swap.id,
                    )
            else:
                credited = await _apply_credit_locked(
                    key,
                    session,
                    amount=swap.destination_amount,
                    unit=swap.destination_unit,
                    mint_url=swap.destination_mint,
                    token=str(swap.token),
                    refund_mint_url=swap.source_mint,
                    swap_id=swap.id,
                )
        except TokenConsumedError:
            if started_melted:
                stored = await _load_swap(swap.id)
                if stored is not None and stored.status == "credited":
                    return credited_msats()
            raise
        swap.status = "credited"
        swap.error = None
        logger.info(
            "Cross-mint swap credited",
            extra={
                "event": "cashu_swap_completed",
                "swap_id": swap.id,
                "source_mint": swap.source_mint,
                "destination_mint": swap.destination_mint,
                "credited_msats": credited,
            },
        )
        return credited


# --- outbound: trusted mint -> user's mint (refund) -------------------------


async def swap_out_for_refund(
    session: AsyncSession, refund: Refund, destination_mint: str
) -> bool:
    """Pay a refund as a token on the user's own (foreign) mint.

    Owner proofs on the preferred trusted mint pay a mint quote on the user's
    mint; the user receives the net amount after the Lightning fee reserve and input
    fees. The ``Refund`` claim carries the melt quote so the existing refund
    reconciler can hold or release the balance; the swap row carries the rest.
    """
    from . import refund as refund_module
    from .payment.lnurl import MeltOutcomeAmbiguousError, MeltUnpaidError

    unit = refund.unit
    amount = refund.amount_msats // 1000 if unit == "sat" else refund.amount_msats
    async with wallet_operation_guard():
        source_mint = await find_trusted_mint_with_funds(
            amount,
            unit,
            _trusted_swap_destination(),
            force_reload=True,
        )
    await _trusted_mint_unit(
        source_mint,
        liability_unit=unit,
        source_unit=unit,
        bolt11_operation="melt",
    )
    try:
        await assert_public_https_origin(destination_mint)
    except BlockedDestinationError as error:
        raise ForeignMintSwapError(str(error)) from error

    async with foreign_mint_lock(destination_mint):
        dest_wallet = await get_wallet(destination_mint, unit, load=False)
        await run_foreign_mint_operation(
            dest_wallet.load_mint_keysets,
            mint_url=destination_mint,
            op_name="refund_swap_load_keysets",
        )
        try:
            await dest_wallet.activate_keyset()
        except Exception as error:
            raise ForeignMintSwapError("Refund mint has no active keyset") from error

        source_wallet = await get_wallet(source_mint, unit)
        async with wallet_operation_guard():
            await source_wallet.load_proofs(reload=True)
            proofs = get_proofs_per_mint_and_unit(
                source_wallet, source_mint, unit, not_reserved=True
            )
            if sum(p.amount for p in proofs) < amount:
                raise ValueError("Trusted mint balance cannot cover this refund")
            selection = source_wallet.coinselect(proofs, amount, include_fees=True)
            input_fees = source_wallet.get_fees_for_proofs(selection or proofs)

        async def quotes(mint_amount: int) -> tuple[MintQuote, MeltQuote]:
            mint_quote = await run_foreign_mint_operation(
                lambda: dest_wallet.request_mint(mint_amount),
                mint_url=destination_mint,
                op_name="refund_swap_request_mint",
            )
            melt_quote = await run_mint_operation(
                lambda: source_wallet.melt_quote(mint_quote.request),
                op_name="refund_swap_melt_quote",
                mint_url=source_mint,
                retry_timeouts=False,
            )
            return mint_quote, melt_quote

        mint_quote, melt_quote = await quotes(amount)
        net = amount - melt_quote.fee_reserve - input_fees
        # Price input fees from the exact proofs the melt will spend: an earlier
        # estimate can miss the fees of a larger offline selection, and the
        # mint then rejects the melt as underfunded.
        for _ in range(5):
            if net <= 0:
                raise ForeignMintSwapError("Refund amount does not cover swap fees")
            mint_quote, melt_quote = await quotes(net)
            async with wallet_operation_guard():
                await source_wallet.load_proofs(reload=True)
                proofs = get_proofs_per_mint_and_unit(
                    source_wallet, source_mint, unit, not_reserved=True
                )
                try:
                    melt_proofs = await select_melt_inputs(
                        source_wallet,
                        proofs,
                        melt_quote.amount + melt_quote.fee_reserve,
                    )
                except Exception:
                    # The pool cannot fund this quote plus its input fees.
                    net -= 1
                    continue
            spent = sum(p.amount for p in melt_proofs)
            if spent <= amount:
                break
            net -= spent - amount
        else:
            raise ForeignMintSwapError("Refund amount does not cover swap fees")
        input_fees = spent - melt_quote.amount - melt_quote.fee_reserve
        melt_secrets = {p.secret for p in melt_proofs}

        swap = CashuSwap(
            direction="out",
            status="melting",
            api_key_hashed_key=refund.api_key_hashed_key,
            refund_id=refund.id,
            source_mint=source_mint,
            source_unit=unit,
            source_amount=amount,
            destination_mint=destination_mint,
            destination_unit=unit,
            destination_amount=net,
            fee_reserve=int(melt_quote.fee_reserve),
            input_fees=input_fees,
            mint_quote_id=mint_quote.quote,
            melt_quote_id=melt_quote.quote,
        )
        await _save(swap)
        await refund_module.record_quote(refund, melt_quote.quote, source_mint)

        async with wallet_operation_guard():
            await source_wallet.load_proofs(reload=True)
            proofs = get_proofs_per_mint_and_unit(
                source_wallet, source_mint, unit, not_reserved=True
            )
            selected = [p for p in proofs if p.secret in melt_secrets]
            if sum(p.amount for p in selected) == spent:
                proofs = selected
            plan = Bolt11PaymentPlan(
                mint_quote.request,
                source_wallet,
                proofs,
                melt_quote,
                source_mint,
                unit,
            )
            try:
                await _execute_bolt11_payment(plan)
            except Bolt11PaymentNotAttempted as error:
                await _update(swap, status="failed", error=str(error))
                raise MeltUnpaidError(str(error)) from error
            except Bolt11PaymentAmbiguous as error:
                # A melt the mint answered with a refusal is not ambiguous; once
                # the mint also reports the quote unpaid, release the balance now.
                if (
                    _melt_rejected_inputs(error) or _melt_definitively_failed(error)
                ) and await _check_bolt11_payment_status_locked(
                    source_mint, unit, melt_quote.quote
                ) == "unpaid":
                    await _update(swap, status="failed", error=str(error))
                    raise MeltUnpaidError(str(error)) from error
                await _update(swap, status="ambiguous", error=str(error))
                await refund_module.hold(session, refund, melt_quote.quote)
                raise MeltOutcomeAmbiguousError(str(error)) from error
        await _update(swap, status="melted", error=None)

        try:
            token = await _issue_refund_token(swap, dest_wallet)
        except Exception as error:
            await _update(swap, error=str(error))
            await refund_module.hold(session, refund, melt_quote.quote)
            logger.error(
                "Refund swap paid but minting on the user's mint failed; held",
                extra={"swap_id": swap.id, "refund_id": refund.id, "error": str(error)},
            )
            raise MeltOutcomeAmbiguousError(
                "Refund token could not be minted yet"
            ) from error

    refund.token = token
    refund.mint_url = destination_mint
    settled = await refund_module.settle(
        session, refund, token=token, mint_url=destination_mint
    )
    await _update(swap, status="settled", error=None)
    return settled


async def _issue_refund_token(swap: CashuSwap, dest_wallet: Wallet) -> str:
    """Mint the paid quote on the user's mint and hand the proofs over as a token."""
    new_proofs = await _mint_with_recovery(
        dest_wallet,
        swap.destination_amount,
        str(swap.mint_quote_id),
        mint_url=swap.destination_mint,
        foreign=True,
    )
    token = await dest_wallet.serialize_proofs(
        new_proofs, include_dleq=False, legacy=False, memo=None
    )
    await dest_wallet.set_reserved_for_send(new_proofs, reserved=True)
    await _update(swap, status="issued", token=token, error=None)
    return token


# --- reconciler ---------------------------------------------------------------


async def _lease(swap_id: str, now: int, cutoff: int) -> bool:
    async with db.create_session() as session:
        result = await session.exec(  # type: ignore[call-overload]
            update(CashuSwap)
            .where(col(CashuSwap.id) == swap_id)
            .where(col(CashuSwap.status).in_(SWAP_OPEN_STATUSES))
            .where(
                col(CashuSwap.claimed_at).is_(None)
                | (col(CashuSwap.claimed_at) < cutoff)
            )
            .values(claimed_at=now)
        )
        await session.commit()
        return bool(result.rowcount)


async def _source_melt_state(swap: CashuSwap) -> str:
    """Ask the foreign mint what became of an inbound swap's melt."""
    async with foreign_mint_lock(swap.source_mint):
        wallet = await get_wallet(swap.source_mint, swap.source_unit, load=False)
        quote = await run_foreign_mint_operation(
            lambda: wallet.get_melt_quote(str(swap.melt_quote_id)),
            mint_url=swap.source_mint,
            op_name="swap_reconcile_melt_quote",
        )
    return _state_name(quote) if quote is not None else "unknown"


async def _reconcile_in(swap: CashuSwap, now: int) -> None:
    if swap.status in ("melting", "ambiguous"):
        if swap.updated_at > now - settings.refund_claim_timeout_seconds:
            return
        state = await _source_melt_state(swap)
        if state == "paid":
            await _update(swap, status="melted", error=None)
        elif state == "unpaid":
            # The mint says it never paid, so the sender still holds the proofs.
            await _update(
                swap,
                status="failed",
                token_hash=None,
                error="melt unpaid at the mint",
            )
            return
        else:
            logger.warning(
                "Inbound swap melt still unresolved",
                extra={"swap_id": swap.id, "melt_state": state},
            )
            return
    if swap.status in ("melted", "minted"):
        try:
            await _finish_swap_in(swap)
        except SwapPendingError as error:
            logger.warning(
                "Inbound swap not finished yet",
                extra={"swap_id": swap.id, "error": str(error)},
            )


async def _reconcile_out(swap: CashuSwap, now: int) -> None:
    from . import refund as refund_module

    async with db.create_session() as session:
        refund = await session.get(Refund, swap.refund_id)
    if refund is None:
        await _update(swap, status="failed", error="refund claim missing")
        return

    if swap.status in ("melting", "ambiguous"):
        if swap.updated_at > now - settings.refund_claim_timeout_seconds:
            return
        async with wallet_operation_guard():
            state = await _check_bolt11_payment_status_locked(
                swap.source_mint, swap.source_unit, str(swap.melt_quote_id)
            )
        if state == "paid":
            await _update(swap, status="melted", error=None)
        elif state == "unpaid":
            await _update(swap, status="failed", error="melt unpaid at the mint")
            async with db.create_session() as session:
                await refund_module.release(session, refund)
            return
        else:
            logger.warning(
                "Refund swap melt still unresolved",
                extra={"swap_id": swap.id, "melt_state": state},
            )
            return

    if swap.status == "melted":
        async with foreign_mint_lock(swap.destination_mint):
            dest_wallet = await get_wallet(
                swap.destination_mint, swap.destination_unit, load=False
            )
            await run_foreign_mint_operation(
                dest_wallet.load_mint_keysets,
                mint_url=swap.destination_mint,
                op_name="refund_swap_reconcile_keysets",
            )
            await dest_wallet.activate_keyset()
            await _issue_refund_token(swap, dest_wallet)

    if swap.status == "issued":
        token = str(swap.token)
        async with db.create_session() as session:
            await refund_module.settle(
                session, refund, token=token, mint_url=swap.destination_mint
            )
        refund.token = token
        refund.mint_url = swap.destination_mint
        await refund_module._record_cashu_payout(refund)
        await _update(swap, status="settled", error=None)


async def reconcile_swaps_once() -> None:
    """Finish or fail swap rows whose request died before a final state."""
    now = int(time.time())
    cutoff = now - settings.refund_claim_timeout_seconds
    async with db.create_session() as session:
        result = await session.exec(
            select(CashuSwap)
            .where(col(CashuSwap.status).in_(SWAP_OPEN_STATUSES))
            .where(
                col(CashuSwap.claimed_at).is_(None)
                | (col(CashuSwap.claimed_at) < cutoff)
            )
            # Melts are only queried once they age past the cutoff; younger
            # rows must not take batch slots from rows that are due.
            .where(
                col(CashuSwap.status).not_in(("melting", "ambiguous"))
                | (col(CashuSwap.updated_at) <= cutoff)
            )
            # Least recently attempted first: rows that never resolve cannot
            # keep newer rows out of the batch.
            .order_by(
                col(CashuSwap.claimed_at).asc().nulls_first(),
                col(CashuSwap.created_at),
            )
            .limit(RECONCILE_BATCH_LIMIT)
        )
        stale = list(result.all())

    for swap in stale:
        if not await _lease(swap.id, now, cutoff):
            continue
        try:
            if swap.direction == "in":
                await _reconcile_in(swap, now)
            else:
                await _reconcile_out(swap, now)
        except Exception as error:
            logger.error(
                "Swap reconciliation failed",
                extra={
                    "swap_id": swap.id,
                    "direction": swap.direction,
                    "error": str(error),
                    "error_type": type(error).__name__,
                },
                exc_info=True,
            )
        finally:
            # A row that stays open keeps its lease stamp as its last attempt,
            # which the batch orders by, so it rotates behind newer rows.
            if swap.status not in SWAP_OPEN_STATUSES:
                await _update(swap, claimed_at=None, updated_at=swap.updated_at)


async def periodic_swap_reconcile() -> None:
    while True:
        await asyncio.sleep(settings.swap_reconcile_interval_seconds)
        try:
            await reconcile_swaps_once()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "Swap reconcile loop error",
                extra={"error": str(error), "error_type": type(error).__name__},
            )
