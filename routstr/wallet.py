import asyncio
import re
import socket
import time
import typing
from typing import TypedDict

import httpx
from cashu.core.base import Proof, Token
from cashu.core.mint_info import MintInfo as _CashuMintInfo
from cashu.wallet.helpers import deserialize_token_from_string
from cashu.wallet.wallet import Wallet
from pydantic_core import PydanticUndefined
from sqlmodel import col, select, update

from .core import db, get_logger
from .core.db import store_cashu_transaction_with_retry as store_cashu_transaction
from .core.settings import settings
from .payment.lnurl import raw_send_to_lnurl

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


class MintConnectionError(Exception):
    """The mint could not be reached (network transport failure).

    Maps to a 503, not a 4xx: the token is fine, the mint is just unavailable.
    """


class TokenConsumedError(Exception):
    """A failure that happened AFTER the token's proofs were spent (melt
    succeeded, or redemption already returned) — e.g. minting on the primary
    mint or the DB credit then failed.

    Non-retryable: the same token will not work again. Seals the cause chain so
    a transport error underneath is never re-surfaced as a retryable
    mint_unreachable.
    """


# httpx base classes cover their subclasses. HTTPStatusError is excluded on
# purpose — that means the mint answered, just with an error status.
_TRANSPORT_EXC_TYPES: tuple[type[BaseException], ...] = (
    httpx.NetworkError,
    httpx.TimeoutException,
    ConnectionError,  # refused/reset/aborted
    socket.gaierror,  # DNS failure
    asyncio.TimeoutError,
)


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
        if isinstance(current, _TRANSPORT_EXC_TYPES):
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
    await wallet.load_mint(keyset_id=token_obj.keysets[0])
    wallet.verify_proofs_dleq(token_obj.proofs)
    input_fees = wallet.get_fees_for_proofs(token_obj.proofs)
    await wallet.split(proofs=token_obj.proofs, amount=0, include_fees=True)
    return int(token_obj.amount) - input_fees, token_obj.unit, token_obj.mint


async def recieve_token(
    token: str,
) -> tuple[int, str, str]:  # amount, unit, mint_url
    token_obj = deserialize_token_from_string(token)
    if len(token_obj.keysets) > 1:
        raise ValueError("Multiple keysets per token currently not supported")

    wallet = await get_wallet(token_obj.mint, token_obj.unit, load=False)
    wallet.keyset_id = token_obj.keysets[0]

    if token_obj.mint not in settings.cashu_mints:
        return await swap_to_primary_mint(token_obj, wallet)

    return await _redeem_same_mint(wallet, token_obj)


async def send(amount: int, unit: str, mint_url: str | None = None) -> tuple[int, str]:
    """Internal send function - returns amount and serialized token"""
    effective_mint_url = mint_url or settings.primary_mint
    wallet: Wallet = await get_wallet(effective_mint_url, unit)
    proofs = get_proofs_per_mint_and_unit(wallet, effective_mint_url, unit)
    proofs_for_mint = sum(p.amount for p in proofs)

    # Fallback: proofs from untrusted source mints are swapped to primary_mint
    # during receive, so the user's preferred refund_mint_url may have no proofs
    # even though the global wallet has the balance.
    if proofs_for_mint < amount and effective_mint_url != settings.primary_mint:
        logger.info(
            f"send: insufficient proofs at {effective_mint_url} "
            f"(have {proofs_for_mint}, need {amount}), falling back to primary_mint={settings.primary_mint}"
        )
        effective_mint_url = settings.primary_mint
        wallet = await get_wallet(effective_mint_url, unit)
        proofs = get_proofs_per_mint_and_unit(wallet, effective_mint_url, unit)
        proofs_for_mint = sum(p.amount for p in proofs)

    all_mint_urls = list({k.mint_url for k in wallet.keysets.values()})
    proof_summary = {
        f"{k.mint_url}/{k.unit.name}": sum(p.amount for p in wallet.proofs if p.id == k.id)
        for k in wallet.keysets.values()
    }
    # Show ALL proofs in DB by keyset_id, regardless of whether the loaded wallet
    # knows about that keyset. This reveals proofs orphaned under stale keysets.
    raw_proofs_by_keyset: dict[str, int] = {}
    for p in wallet.proofs:
        raw_proofs_by_keyset[p.id] = raw_proofs_by_keyset.get(p.id, 0) + p.amount
    logger.info(
        f"send: proof inventory | mint={effective_mint_url} unit={unit} amount={amount} "
        f"primary_mint={settings.primary_mint} proofs_for_mint={proofs_for_mint} "
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
    fee_msat = fees * 1000 if token_unit == "sat" else fees
    remaining_msat = amount_msat - fee_msat
    if settings.primary_mint_unit == "sat":
        return int(remaining_msat // 1000)
    return int(remaining_msat)


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


async def _calculate_swap_amount(
    amount_msat: int,
    token_unit: str,
    token_mint_url: str,
    token_wallet: Wallet,
    primary_wallet: Wallet,
    proofs: list,
) -> int:
    """
    Calculate the amount to mint on the primary mint after accounting for
    melt fees and NUT-02 input fees on the foreign mint.
    """
    if settings.primary_mint_unit == "sat":
        receive_amount = amount_msat // 1000
    else:
        receive_amount = amount_msat

    if token_mint_url == settings.primary_mint:
        logger.info(
            "swap_to_primary_mint: skipping fee estimation (same mint)",
            extra={"minted_amount": receive_amount},
        )
        return int(receive_amount)

    logger.info(
        "swap_to_primary_mint: estimating fees",
        extra={
            "dummy_amount": receive_amount,
            "unit": settings.primary_mint_unit,
        },
    )

    try:
        dummy_mint_quote = await primary_wallet.request_mint(receive_amount)
        dummy_melt_quote = await token_wallet.melt_quote(dummy_mint_quote.request)

        fee_reserve = dummy_melt_quote.fee_reserve
        input_fees = token_wallet.get_fees_for_proofs(proofs)
        total_fees = fee_reserve + input_fees
        minted_amount = _net_minted_amount(amount_msat, token_unit, total_fees)

        if minted_amount <= 0:
            raise ValueError(f"Fees ({total_fees} {token_unit}) exceed token amount")

        logger.info(
            "swap_to_primary_mint: fee estimation result",
            extra={
                "token_amount_sat": amount_msat // 1000,
                "estimated_fee": total_fees,
                "estimated_fee_unit": token_unit,
                "input_fees": input_fees,
                "minted_amount": minted_amount,
                "minted_unit": settings.primary_mint_unit,
            },
        )
        return minted_amount

    except Exception as e:
        logger.error(
            "swap_to_primary_mint: fee estimation failed",
            extra={"error": str(e)},
        )
        if is_mint_connection_error(e):
            raise MintConnectionError("Cashu mint is unreachable") from e
        raise ValueError(f"Failed to estimate fees: {e}") from e


async def swap_to_primary_mint(
    token_obj: Token, token_wallet: Wallet
) -> tuple[int, str, str]:
    logger.info(
        "swap_to_primary_mint: starting",
        extra={
            "foreign_mint": token_obj.mint,
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
        amount_msat = token_amount * 1000
    elif token_obj.unit == "msat":
        amount_msat = token_amount
    else:
        raise ValueError("Invalid unit")
    # If the token is already from the primary mint, we don't need a cross-mint
    # swap — redeem it same-mint. There's no melt/Lightning fee, but the mint's
    # NUT-02 input fee still applies; _redeem_same_mint accounts for it.
    if token_obj.mint == settings.primary_mint:
        logger.info(
            "swap_to_primary_mint: token already on primary mint, skipping swap",
            extra={
                "mint": token_obj.mint,
                "amount": token_amount,
                "unit": token_obj.unit,
            },
        )
        return await _redeem_same_mint(token_wallet, token_obj)

    primary_wallet = await get_wallet(settings.primary_mint, settings.primary_mint_unit)

    minted_amount = await _calculate_swap_amount(
        amount_msat,
        token_obj.unit,
        token_obj.mint,
        token_wallet,
        primary_wallet,
        token_obj.proofs,
    )

    # The estimate above is non-binding: the mint may demand a higher fee on the
    # real quote or reject the melt outright. Retry the quote/melt cycle with the
    # amount recomputed from the fees the mint actually demands.
    observed_extra_fee = 0
    attempt = 0
    while True:
        attempt += 1
        mint_quote = await primary_wallet.request_mint(minted_amount)
        logger.info(
            "swap_to_primary_mint: mint quote received",
            extra={"mint_quote_id": mint_quote.quote, "attempt": attempt},
        )

        melt_quote = await token_wallet.melt_quote(mint_quote.request)
        input_fees = token_wallet.get_fees_for_proofs(token_obj.proofs)
        total_needed = melt_quote.amount + melt_quote.fee_reserve + input_fees
        logger.info(
            "swap_to_primary_mint: melt quote received",
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
                    "swap_to_primary_mint: insufficient token amount for melt fees",
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
                "swap_to_primary_mint: melt quote exceeds token amount, retrying",
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
            _ = await token_wallet.melt(
                proofs=token_obj.proofs,
                invoice=mint_quote.request,
                fee_reserve_sat=melt_quote.fee_reserve,
                quote_id=melt_quote.quote,
            )
        except Exception as e:
            # A down mint won't fix itself by retrying with a smaller amount.
            if is_mint_connection_error(e):
                logger.error(
                    "swap_to_primary_mint: melt failed — mint unreachable",
                    extra={"error": str(e), "foreign_mint": token_obj.mint},
                )
                raise MintConnectionError("Cashu mint is unreachable") from e
            shortfall = _melt_insufficient_shortfall(e)
            recomputed = 0
            if shortfall is not None:
                observed_extra_fee += shortfall
                recomputed = _net_minted_amount(
                    amount_msat,
                    token_obj.unit,
                    melt_quote.fee_reserve + input_fees + observed_extra_fee,
                )
            if shortfall is None or attempt >= _MAX_SWAP_ATTEMPTS or recomputed <= 0:
                logger.error(
                    "swap_to_primary_mint: melt failed",
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
                "swap_to_primary_mint: mint demanded more than quoted at melt, retrying",
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
        "swap_to_primary_mint: melt succeeded, minting on primary",
        extra={"minted_amount": minted_amount, "mint_quote_id": mint_quote.quote},
    )

    await primary_wallet.load_proofs(reload=True)
    pre_mint_balance = primary_wallet.available_balance.amount
    try:
        _ = await primary_wallet.mint(minted_amount, quote_id=mint_quote.quote)
    except Exception as e:
        if "11003" in str(e) or "outputs already signed" in str(e).lower():
            # Previous mint call signed outputs at the mint but failed before
            # bump_secret_derivation ran locally. Recover orphaned proofs and
            # advance the counter so the next request derives fresh secrets.
            logger.warning(
                "swap_to_primary_mint: outputs already signed — recovering orphaned proofs",
                extra={"mint_quote_id": mint_quote.quote, "minted_amount": minted_amount},
            )
            try:
                for keyset_id in primary_wallet.keysets:
                    await primary_wallet.restore_tokens_for_keyset(keyset_id, to=1, batch=25)
                await primary_wallet.load_proofs(reload=True)
                post_recovery_balance = primary_wallet.available_balance.amount
                balance_gained = post_recovery_balance - pre_mint_balance
                logger.info(
                    "swap_to_primary_mint: recovery scan completed",
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
                    "swap_to_primary_mint: recovery failed",
                    extra={"error": str(recovery_err)},
                )
                raise TokenConsumedError(
                    f"Mint on primary failed and recovery unsuccessful: {e}"
                ) from e
        else:
            logger.error(
                "swap_to_primary_mint: mint on primary failed after successful melt",
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
        "swap_to_primary_mint: completed successfully",
        extra={
            "foreign_mint": token_obj.mint,
            "primary_mint": settings.primary_mint,
            "original_amount": token_amount,
            "minted_amount": minted_amount,
            "unit": settings.primary_mint_unit,
        },
    )

    return int(minted_amount), settings.primary_mint_unit, settings.primary_mint


async def credit_balance(
    cashu_token: str, key: db.ApiKey, session: db.AsyncSession
) -> int:
    logger.info(
        "credit_balance: Starting token redemption",
        extra={"token_preview": cashu_token[:50]},
    )

    try:
        amount, unit, mint_url = await recieve_token(cashu_token)
        original_amount = amount
        original_unit = unit
        logger.info(
            "credit_balance: Token redeemed successfully",
            extra={"amount": amount, "unit": unit, "mint_url": mint_url},
        )

        if unit == "sat":
            amount = amount * 1000
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
            stmt = (
                update(db.ApiKey)
                .where(col(db.ApiKey.hashed_key) == key.hashed_key)
                .values(balance=(db.ApiKey.balance) + amount)
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

        try:
            await store_cashu_transaction(
                token=cashu_token,
                amount=original_amount,
                unit=original_unit,
                mint_url=mint_url,
                typ="in",
                source="apikey",
                api_key_hashed_key=key.hashed_key,
            )
        except Exception:
            pass
        else:
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


async def get_wallet(mint_url: str, unit: str = "sat", load: bool = True) -> Wallet:
    global _wallets
    id = f"{mint_url}_{unit}"
    if id not in _wallets:
        _wallets[id] = await Wallet.with_db(mint_url, db=".wallet", unit=unit)

    if load:
        await _wallets[id].load_mint()
        await _wallets[id].load_proofs(reload=True)
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


async def slow_filter_spend_proofs(proofs: list[Proof], wallet: Wallet) -> list[Proof]:
    if not proofs:
        return []
    _proofs = []
    _spent_proofs = []
    for i in range(0, len(proofs), 1000):
        pb = proofs[i : i + 1000]
        proof_states = await wallet.check_proof_state(pb)
        for proof, state in zip(pb, proof_states.states):
            if str(state.state) != "spent":
                _proofs.append(proof)
            else:
                _spent_proofs.append(proof)
    await wallet.set_reserved_for_send(_spent_proofs, reserved=True)
    return _proofs


class BalanceDetail(TypedDict, total=False):
    mint_url: str
    unit: str
    wallet_balance: int
    user_balance: int
    owner_balance: int
    error: str


async def fetch_all_balances(
    units: list[str] | None = None,
) -> tuple[list[BalanceDetail], int, int, int]:
    """
    Fetch balances for all trusted mints and units concurrently.

    Returns:
        - List of balance details for each mint/unit combination
        - Total wallet balance in sats
        - Total user balance in sats
        - Owner balance in sats (wallet - user)
    """
    if units is None:
        units = ["sat", "msat"]

    async def fetch_balance(
        session: db.AsyncSession, mint_url: str, unit: str
    ) -> BalanceDetail:
        try:
            wallet = await get_wallet(mint_url, unit)
            proofs = get_proofs_per_mint_and_unit(
                wallet, mint_url, unit, not_reserved=True
            )
            proofs = await slow_filter_spend_proofs(proofs, wallet)
            user_balance = await db.balances_for_mint_and_unit(session, mint_url, unit)
            if unit == "sat":
                user_balance = user_balance // 1000
            proofs_balance = sum(proof.amount for proof in proofs)

            result: BalanceDetail = {
                "mint_url": mint_url,
                "unit": unit,
                "wallet_balance": proofs_balance,
                "user_balance": user_balance,
                "owner_balance": proofs_balance - user_balance if proofs_balance != 0 else 0,
            }
            return result
        except Exception as e:
            logger.error(f"Error getting balance for {mint_url} {unit}: {e}")
            error_result: BalanceDetail = {
                "mint_url": mint_url,
                "unit": unit,
                "wallet_balance": 0,
                "user_balance": 0,
                "owner_balance": 0,
                "error": str(e),
            }
            return error_result

    # Build the set of mints to inspect. Received tokens are stored against
    # ``primary_mint`` (which defaults to a real mint even when ``cashu_mints``
    # is empty), so include it as a fallback — otherwise a node that accepts
    # payments would still report empty balances when ``cashu_mints`` is unset.
    mint_urls: list[str] = list(settings.cashu_mints)
    if settings.primary_mint and settings.primary_mint not in mint_urls:
        mint_urls.append(settings.primary_mint)

    # Create tasks for all mint/unit combinations
    async with db.create_session() as session:
        tasks = [
            fetch_balance(session, mint_url, unit)
            for mint_url in mint_urls
            for unit in units
        ]

        # Run all tasks concurrently
        balance_details = list(await asyncio.gather(*tasks))

    # Calculate totals
    total_wallet_balance_sats = 0
    total_user_balance_sats = 0

    for detail in balance_details:
        if not detail.get("error"):
            # Convert to sats for total calculation
            unit = detail["unit"]
            proofs_balance_sats = (
                detail["wallet_balance"]
                if unit == "sat"
                else detail["wallet_balance"] // 1000
            )
            user_balance_sats = (
                detail["user_balance"]
                if unit == "sat"
                else detail["user_balance"] // 1000
            )

            total_wallet_balance_sats += proofs_balance_sats
            total_user_balance_sats += user_balance_sats

    owner_balance = 0
    if total_wallet_balance_sats != 0:
        owner_balance = total_wallet_balance_sats - total_user_balance_sats

    return (
        balance_details,
        total_wallet_balance_sats,
        total_user_balance_sats,
        owner_balance,
    )


async def periodic_payout() -> None:
    while True:
        await asyncio.sleep(settings.payout_interval_seconds)
        try:
            if not settings.receive_ln_address:
                continue

            # Include the primary mint even if it is not listed in cashu_mints,
            # matching fetch_all_balances(); otherwise primary-mint funds never
            # auto-payout.
            mint_urls: list[str] = list(settings.cashu_mints)
            if settings.primary_mint and settings.primary_mint not in mint_urls:
                mint_urls.append(settings.primary_mint)

            async with db.create_session() as session:
                for mint_url in mint_urls:
                    for unit in ["sat", "msat"]:
                        # Isolate failures per mint/unit so one slow or failing
                        # mint does not abort payout for every other mint/unit.
                        try:
                            wallet = await get_wallet(mint_url, unit)
                            proofs = get_proofs_per_mint_and_unit(
                                wallet, mint_url, unit, not_reserved=True
                            )
                            proofs = await slow_filter_spend_proofs(proofs, wallet)
                            await asyncio.sleep(5)
                            user_balance = await db.balances_for_mint_and_unit(
                                session, mint_url, unit
                            )
                            if unit == "sat":
                                user_balance = user_balance // 1000
                            proofs_balance = sum(proof.amount for proof in proofs)
                            available_balance = proofs_balance - user_balance
                            # Threshold is configured in sats; convert for msat wallets.
                            min_amount = (
                                settings.min_payout_sat
                                if unit == "sat"
                                else settings.min_payout_sat * 1000
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
                                extra={
                                    "error": str(e),
                                    "mint_url": mint_url,
                                    "unit": unit,
                                },
                            )
        except Exception as e:
            logger.error(
                f"Error in periodic payout cycle: {type(e).__name__}",
                extra={"error": str(e)},
            )


async def _refund_sweep_once(cutoff: int) -> None:
    async with db.create_session() as session:
        stmt = select(db.CashuTransaction).where(
            db.CashuTransaction.type == "out",
            db.CashuTransaction.collected == False,  # noqa: E712
            db.CashuTransaction.swept == False,  # noqa: E712
            db.CashuTransaction.created_at < cutoff,
        )
        results = await session.exec(stmt)
        refunds = results.all()

        for refund in refunds:
            try:
                await recieve_token(refund.token)
                refund.swept = True
                session.add(refund)
                logger.info(
                    "Swept uncollected refund",
                    extra={
                        "id": refund.id,
                        "amount": refund.amount,
                        "unit": refund.unit,
                    },
                )
            except Exception as e:
                error_msg = str(e).lower()
                if "already spent" in error_msg:
                    refund.collected = True
                    session.add(refund)
                    logger.info(
                        "Refund already spent (client collected), marking swept",
                        extra={
                            "id": refund.id,
                        },
                    )
                else:
                    logger.warning(
                        "Failed to sweep refund",
                        extra={
                            "id": refund.id,
                            "error": str(e),
                        },
                    )
        await session.commit()


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
                if fee.payout_in_progress_msats:
                    logger.critical(
                        "Routstr fee payout requires manual reconciliation",
                        extra={
                            "payout_in_progress_msats": fee.payout_in_progress_msats,
                            "payout_started_at": fee.payout_started_at,
                        },
                    )
                    continue

                accumulated_sats = fee.accumulated_msats // 1000
                if accumulated_sats >= ROUTSTR_FEE_DEFAULT_PAYOUT:
                    wallet = await get_wallet(settings.primary_mint, "sat")
                    proofs = get_proofs_per_mint_and_unit(
                        wallet, settings.primary_mint, "sat", not_reserved=True
                    )
                    paid_msats = accumulated_sats * 1000
                    payout_checkpointed = await db.reset_routstr_fee(
                        session, paid_msats
                    )
                    if not payout_checkpointed:
                        logger.warning("Routstr fee payout was already claimed")
                        continue

                    try:
                        amount_received = await raw_send_to_lnurl(
                            wallet,
                            proofs,
                            ROUTSTR_LN_ADDRESS,
                            "sat",
                            amount=accumulated_sats,
                        )
                    except Exception:
                        logger.critical(
                            "Routstr fee payout outcome is unknown; manual reconciliation required",
                            extra={"payout_in_progress_msats": paid_msats},
                            exc_info=True,
                        )
                        continue

                    payout_completed = await db.complete_routstr_fee_payout(
                        session, paid_msats
                    )
                    if not payout_completed:
                        logger.critical(
                            "Routstr fee payout sent but checkpoint was not completed",
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
    wallet = await get_wallet(mint, unit)
    proofs = wallet._get_proofs_per_keyset(wallet.proofs)[wallet.keyset_id]
    proofs, _ = await wallet.select_to_send(proofs, amount, set_reserved=True)
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
