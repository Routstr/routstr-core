import asyncio
import hashlib
import math
import random
import time
import uuid
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from fastapi import HTTPException
from sqlalchemy import case, inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select, update

from .core import get_logger
from .core.db import (
    ApiKey,
    AsyncSession,
    ReservationRelease,
    accumulate_routstr_fee,
    create_session,
)
from .core.settings import settings
from .payment.cost_calculation import (
    CostData,
    CostDataError,
    MaxCostData,
    calculate_cost,
)
from .redemption_cache import (
    TERMINAL_REDEMPTION_CODES,
    CachedRedemptionFailure,
    redemption_negative_cache,
)
from .wallet import (
    classify_redemption_error,
    credit_balance,
    deserialize_token_from_string,
    wallet_operation_guard,
)

if TYPE_CHECKING:
    from .payment.models import Model

logger = get_logger(__name__)
payments_logger = get_logger("routstr.payments")

# Routstr platform fee constants
ROUTSTR_FEE_PERCENT: float = 2.1
ROUTSTR_LN_ADDRESS: str = (
    "npub130mznv74rxs032peqym6g3wqavh472623mt3z5w73xq9r6qqdufs7ql29s@npub.cash"
)
ROUTSTR_FEE_PAYOUT_INTERVAL_SECONDS: int = 900
ROUTSTR_FEE_DEFAULT_PAYOUT: int = 200


def _format_msat_amount(amount: int) -> str:
    sats = f"{amount / 1000:.3f}".rstrip("0").rstrip(".")
    return f"{sats} sats ({amount} msats)"


def _model_balance_error(required: int, available: int) -> dict[str, dict[str, str]]:
    return {
        "error": {
            "message": (
                f"Insufficient balance: {_format_msat_amount(required)} required "
                f"for this model; {_format_msat_amount(available)} available."
            ),
            "type": "insufficient_quota",
            "code": "insufficient_balance",
        }
    }


@dataclass(frozen=True)
class ReservationSnapshot:
    release_id: str
    key_hash: str
    billing_key_hash: str
    reserved_msats: int


_current_reservation: ContextVar[ReservationSnapshot | None] = ContextVar(
    "current_billing_reservation", default=None
)


def _clear_current_reservation(snapshot: ReservationSnapshot) -> None:
    current = _current_reservation.get()
    if current is not None and current.release_id == snapshot.release_id:
        _current_reservation.set(None)


# TODO: implement prepaid api key (not like it was before)
# PREPAID_API_KEY = os.environ.get("PREPAID_API_KEY", None)
# PREPAID_BALANCE = int(os.environ.get("PREPAID_BALANCE", "0")) * 1000  # Convert to msats


async def check_and_reset_limit(key: ApiKey, session: AsyncSession) -> bool:
    """Checks if a key's balance limit should be reset based on its policy."""
    if key.balance_limit is not None and key.balance_limit_reset:
        now = int(time.time())
        reset_date = key.balance_limit_reset_date or 0
        should_reset = False

        if key.balance_limit_reset == "daily":
            if (
                datetime.fromtimestamp(now).date()
                > datetime.fromtimestamp(reset_date).date()
            ):
                should_reset = True
        elif key.balance_limit_reset == "weekly":
            if (
                datetime.fromtimestamp(now).isocalendar()[:2]
                > datetime.fromtimestamp(reset_date).isocalendar()[:2]
            ):
                should_reset = True
        elif key.balance_limit_reset == "monthly":
            dt_now = datetime.fromtimestamp(now)
            dt_reset = datetime.fromtimestamp(reset_date)
            if dt_now.year > dt_reset.year or dt_now.month > dt_reset.month:
                should_reset = True

        if should_reset:
            logger.info(
                "Resetting balance limit for key",
                extra={
                    "key_hash": key.hashed_key[:8] + "...",
                    "policy": key.balance_limit_reset,
                    "old_spent": key.total_spent,
                },
            )
            key.total_spent = 0
            key.balance_limit_reset_date = now
            session.add(key)
            await session.flush()
            return True
    return False


def redemption_error_to_http_exception(error: Exception) -> HTTPException:
    """Map a Cashu token redemption failure to a sanitized client-facing error.

    Thin wrapper over the shared :func:`classify_redemption_error` so the bearer
    path stays identical to the X-Cashu and top-up paths.
    """
    classified = classify_redemption_error(error)
    if classified is None:
        return HTTPException(
            status_code=500,
            detail={
                "error": {
                    "message": "Internal error during token redemption",
                    "type": "api_error",
                    "code": "internal_error",
                }
            },
        )
    error_type, status_code, message, error_code = classified
    return HTTPException(
        status_code=status_code,
        detail={
            "error": {
                "message": message,
                "type": error_type,
                "code": error_code,
            }
        },
    )


def _cached_failure_to_http_exception(
    failure: CachedRedemptionFailure,
) -> HTTPException:
    """Rebuild the exact error envelope the original mint-backed failure produced."""
    return HTTPException(
        status_code=failure.status_code,
        detail={
            "error": {
                "message": failure.message,
                "type": failure.error_type,
                "code": failure.code,
            }
        },
    )


def _maybe_cache_terminal_redemption_failure(hashed_key: str, error: Exception) -> None:
    """Record a redemption failure in the negative cache if it can never succeed.

    Transient classifications (mint unreachable, rate-limited) are never
    cached — only codes in TERMINAL_REDEMPTION_CODES, which are permanent
    properties of the token itself.
    """
    classified = classify_redemption_error(error)
    if classified is None:
        return
    error_type, status_code, message, code = classified
    if code not in TERMINAL_REDEMPTION_CODES:
        return
    redemption_negative_cache.put(
        hashed_key,
        CachedRedemptionFailure(
            status_code=status_code,
            error_type=error_type,
            message=message,
            code=code,
        ),
    )
    logger.info(
        "Cached terminal redemption failure; further attempts rejected locally",
        extra={"key_hash": hashed_key[:8] + "...", "code": code},
    )


async def validate_bearer_key(
    bearer_key: str,
    session: AsyncSession,
    refund_address: Optional[str] = None,
    key_expiry_time: Optional[int] = None,
    min_cost: int = 0,
) -> ApiKey:
    if bearer_key.startswith("cashu"):
        # Acquire before the first lookup/flush so concurrent token creation
        # cannot hold SQLite write transactions while waiting to mutate proofs.
        async with wallet_operation_guard():
            return await _validate_bearer_key_locked(
                bearer_key,
                session,
                refund_address,
                key_expiry_time,
                min_cost,
            )
    return await _validate_bearer_key_locked(
        bearer_key, session, refund_address, key_expiry_time, min_cost
    )


async def _validate_bearer_key_locked(
    bearer_key: str,
    session: AsyncSession,
    refund_address: Optional[str] = None,
    key_expiry_time: Optional[int] = None,
    min_cost: int = 0,
) -> ApiKey:
    """
    Validates the provided API key using SQLModel.
    If it's a cashu key, it redeems it and stores its hash and balance.
    Otherwise checks if the hash of the key exists.
    Includes a balance check against min_cost for limited keys.
    """
    logger.debug(
        "Starting bearer key validation",
        extra={
            "key_preview": bearer_key[:20] + "..."
            if len(bearer_key) > 20
            else bearer_key,
            "has_refund_address": bool(refund_address),
            "has_expiry_time": bool(key_expiry_time),
            "min_cost": min_cost,
        },
    )

    if not bearer_key:
        logger.error("Empty bearer key provided")
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": "API key or Cashu token required",
                    "type": "invalid_request_error",
                    "code": "missing_api_key",
                }
            },
        )

    if bearer_key.startswith("sk-"):
        logger.debug(
            "Processing sk- prefixed API key",
            extra={"key_preview": bearer_key[:10] + "..."},
        )

        if existing_key := await session.get(ApiKey, bearer_key[3:]):
            logger.info(
                "Existing sk- API key found",
                extra={
                    "key_hash": existing_key.hashed_key[:8] + "...",
                    "balance": existing_key.balance,
                    "total_requests": existing_key.total_requests,
                },
            )

            if key_expiry_time is not None:
                existing_key.key_expiry_time = key_expiry_time
                logger.debug(
                    "Updated key expiry time",
                    extra={
                        "key_hash": existing_key.hashed_key[:8] + "...",
                        "expiry_time": key_expiry_time,
                    },
                )

            if refund_address is not None:
                existing_key.refund_address = refund_address
                logger.debug(
                    "Updated refund address",
                    extra={
                        "key_hash": existing_key.hashed_key[:8] + "...",
                        "refund_address_preview": refund_address[:20] + "..."
                        if len(refund_address) > 20
                        else refund_address,
                    },
                )

            # Check and reset limit if needed
            await check_and_reset_limit(existing_key, session)

            # Early check: Billing balance check (Parent balance)
            billing_key = await get_billing_key(existing_key, session)
            if min_cost > 0 and billing_key.total_balance < min_cost:
                logger.warning(
                    "Insufficient billing balance during validation",
                    extra={
                        "key_hash": existing_key.hashed_key[:8] + "...",
                        "billing_key_hash": billing_key.hashed_key[:8] + "...",
                        "balance": billing_key.total_balance,
                        "required": min_cost,
                    },
                )
                raise HTTPException(
                    status_code=402,
                    detail=_model_balance_error(min_cost, billing_key.total_balance),
                )

            # Early check: Spending limit check (Child key limit)
            if (
                min_cost > 0
                and existing_key.balance_limit is not None
                and existing_key.total_spent + existing_key.reserved_balance + min_cost
                > existing_key.balance_limit
            ):
                raise HTTPException(
                    status_code=402,
                    detail={
                        "error": {
                            "message": f"Balance limit exceeded: {existing_key.balance_limit} mSats limit. {existing_key.total_spent} already spent ({existing_key.reserved_balance} reserved), {min_cost} minimum required for this model.",
                            "type": "insufficient_quota",
                            "code": "balance_limit_exceeded",
                        }
                    },
                )

            return existing_key
        else:
            logger.warning(
                "sk- API key not found in database",
                extra={"key_preview": bearer_key[:10] + "..."},
            )

    if bearer_key.startswith("cashu"):
        logger.debug(
            "Processing Cashu token",
            extra={
                "token_preview": bearer_key[:20] + "...",
                "token_type": bearer_key[:6] if len(bearer_key) >= 6 else bearer_key,
            },
        )

        try:
            hashed_key = hashlib.sha256(bearer_key.encode()).hexdigest()
            try:
                token_obj = deserialize_token_from_string(bearer_key)
            except Exception as decode_error:
                # A malformed token is a bad token (400 invalid_cashu_token via
                # the shared taxonomy), not an auth failure (401) — otherwise it
                # would fall through to the generic "Invalid API key" handler.
                raise redemption_error_to_http_exception(
                    ValueError(
                        f"Invalid Cashu token: could not decode token ({decode_error})"
                    )
                ) from decode_error
            logger.debug(
                "Generated token hash", extra={"hash_preview": hashed_key[:16] + "..."}
            )

            if existing_key := await session.get(ApiKey, hashed_key):
                logger.info(
                    "Existing Cashu token found",
                    extra={
                        "key_hash": existing_key.hashed_key[:8] + "...",
                        "balance": existing_key.balance,
                        "total_requests": existing_key.total_requests,
                    },
                )

                if key_expiry_time is not None:
                    existing_key.key_expiry_time = key_expiry_time
                    logger.debug(
                        "Updated key expiry time for existing Cashu key",
                        extra={
                            "key_hash": existing_key.hashed_key[:8] + "...",
                            "expiry_time": key_expiry_time,
                        },
                    )

                if refund_address is not None:
                    existing_key.refund_address = refund_address
                    logger.debug(
                        "Updated refund address for existing Cashu key",
                        extra={
                            "key_hash": existing_key.hashed_key[:8] + "...",
                            "refund_address_preview": refund_address[:20] + "..."
                            if len(refund_address) > 20
                            else refund_address,
                        },
                    )

                # Early check: Billing balance check
                if min_cost > 0 and existing_key.total_balance < min_cost:
                    raise HTTPException(
                        status_code=402,
                        detail=_model_balance_error(
                            min_cost, existing_key.total_balance
                        ),
                    )

                return existing_key

            if cached_failure := redemption_negative_cache.get(hashed_key):
                logger.info(
                    "Rejecting known-dead Cashu token from negative cache",
                    extra={
                        "key_hash": hashed_key[:8] + "...",
                        "code": cached_failure.code,
                    },
                )
                raise _cached_failure_to_http_exception(cached_failure)

            logger.info(
                "Creating new Cashu token entry",
                extra={
                    "hash_preview": hashed_key[:16] + "...",
                    "has_refund_address": bool(refund_address),
                    "has_expiry_time": bool(key_expiry_time),
                },
            )
            if token_obj.mint == settings.primary_mint:
                if token_obj.unit != settings.primary_mint_unit:
                    raise redemption_error_to_http_exception(
                        ValueError(
                            "Cashu token unit does not match the configured primary "
                            f"mint unit: expected {settings.primary_mint_unit}, "
                            f"got {token_obj.unit}"
                        )
                    )
                refund_currency = token_obj.unit
                refund_mint_url = settings.primary_mint
            elif token_obj.mint in settings.cashu_mints:
                refund_currency = token_obj.unit
                refund_mint_url = token_obj.mint
            else:
                # Foreign tokens are swapped into the configured primary mint.
                refund_currency = settings.primary_mint_unit
                refund_mint_url = settings.primary_mint

            new_key = ApiKey(
                hashed_key=hashed_key,
                balance=0,
                refund_address=refund_address,
                key_expiry_time=key_expiry_time,
                refund_currency=refund_currency,
                refund_mint_url=refund_mint_url,
            )
            session.add(new_key)

            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                logger.info(
                    "Concurrent key creation detected, fetching existing key",
                    extra={"key_hash": hashed_key[:8] + "..."},
                )
                existing_key = await session.get(ApiKey, hashed_key)
                if not existing_key:
                    raise Exception("Failed to fetch existing key after IntegrityError")

                if key_expiry_time is not None:
                    existing_key.key_expiry_time = key_expiry_time
                if refund_address is not None:
                    existing_key.refund_address = refund_address

                return existing_key

            logger.debug(
                "New key created, starting token redemption",
                extra={"key_hash": hashed_key[:8] + "..."},
            )

            logger.debug(
                "AUTH: About to call credit_balance",
                extra={"token_preview": bearer_key[:50]},
            )
            try:
                msats = await credit_balance(bearer_key, new_key, session)
                logger.debug(
                    "AUTH: credit_balance returned successfully", extra={"msats": msats}
                )
            except Exception as credit_error:
                classification = classify_redemption_error(credit_error)
                expected_codes = {
                    "cashu_token_already_spent",
                    "cashu_source_mint_unreachable",
                    "cashu_mint_unreachable",
                    "cashu_mint_rate_limited",
                }
                log = (
                    logger.info
                    if classification is not None
                    and classification[3] in expected_codes
                    else logger.error
                )
                log(
                    "AUTH: credit_balance failed",
                    extra={
                        "error": str(credit_error),
                        "error_type": type(credit_error).__name__,
                        "error_code": classification[3] if classification else None,
                    },
                )
                await session.rollback()
                _maybe_cache_terminal_redemption_failure(hashed_key, credit_error)
                raise redemption_error_to_http_exception(credit_error) from credit_error

            if msats <= 0:
                logger.error(
                    "Token redemption returned zero or negative amount",
                    extra={"msats": msats, "key_hash": hashed_key[:8] + "..."},
                )
                # Defense-in-depth: credit_balance already raises
                # ValueError("Redeemed token amount must be positive…") before
                # returning (wallet.py), so this branch is only reachable if a
                # zero/negative row was somehow persisted; drop it so we never
                # leave an orphan zero-balance key. Reuse the shared taxonomy
                # (cashu_error) so the envelope matches the mapper above.
                await session.delete(new_key)
                await session.commit()
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": {
                            "message": "Failed to redeem Cashu token: token yielded no value",
                            "type": "cashu_error",
                            "code": "cashu_token_zero_value",
                        }
                    },
                )

            await session.refresh(new_key)
            await session.commit()

            logger.info(
                "New Cashu token successfully redeemed and stored",
                extra={
                    "key_hash": hashed_key[:8] + "...",
                    "redeemed_msats": msats,
                    "final_balance": new_key.balance,
                },
            )

            return new_key
        except HTTPException:
            raise
        except Exception as e:
            await session.rollback()
            logger.error(
                "Cashu token redemption failed",
                extra={
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "token_preview": bearer_key[:20] + "..."
                    if len(bearer_key) > 20
                    else bearer_key,
                },
            )
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "message": "Invalid or expired Cashu key",
                        "type": "invalid_request_error",
                        "code": "invalid_api_key",
                    }
                },
            )

    key_preview = bearer_key[:10] + "..." if len(bearer_key) > 10 else bearer_key
    logger.error(
        f"Invalid API key format: preview={key_preview!r} length={len(bearer_key)} "
        f"(expected 'sk-...' or 'cashu...' token)",
        extra={
            "key_preview": key_preview,
            "key_length": len(bearer_key),
        },
    )

    raise HTTPException(
        status_code=401,
        detail={
            "error": {
                "message": "Invalid API key format. Expected an 'sk-...' API key or a 'cashu...' token.",
                "type": "invalid_request_error",
                "code": "invalid_api_key",
            }
        },
    )


async def get_billing_key(key: ApiKey, session: AsyncSession) -> ApiKey:
    """Returns the key that should be charged for the request."""
    if key.parent_key_hash:
        parent = await session.get(ApiKey, key.parent_key_hash)
        if parent:
            # We want to keep the total_requests and total_spent on the child key
            # but use the balance and reserved_balance of the parent.
            # However, pay_for_request updates reserved_balance and total_requests.
            # To stay simple, we charge the parent's balance and update parent's total_requests.
            return parent
        else:
            logger.error(
                "Parent key not found for child key",
                extra={
                    "child_key_hash": key.hashed_key[:8] + "...",
                    "parent_key_hash": key.parent_key_hash[:8] + "...",
                },
            )
    return key


async def pay_for_request(
    key: ApiKey, cost_per_request: int, session: AsyncSession
) -> ReservationSnapshot:
    """Reserve funds and return the durable identity for this request."""
    # Ensure cost_per_request is at least the minimum allowed request cost
    cost_per_request = max(cost_per_request, settings.min_request_msat)

    billing_key = await get_billing_key(key, session)

    logger.info(
        "Processing payment for request",
        extra={
            "key_hash": key.hashed_key[:8] + "...",
            "billing_key_hash": billing_key.hashed_key[:8] + "...",
            "current_balance": billing_key.balance,
            "required_cost": cost_per_request,
            "sufficient_balance": billing_key.balance >= cost_per_request,
        },
    )

    if billing_key.total_balance < cost_per_request:
        logger.warning(
            "Insufficient balance for request",
            extra={
                "key_hash": key.hashed_key[:8] + "...",
                "billing_key_hash": billing_key.hashed_key[:8] + "...",
                "balance": billing_key.balance,
                "reserved_balance": billing_key.reserved_balance,
                "required": cost_per_request,
                "shortfall": cost_per_request - billing_key.total_balance,
            },
        )

        raise HTTPException(
            status_code=402,
            detail={
                "error": {
                    "message": f"Insufficient balance: {cost_per_request} mSats required. {billing_key.total_balance} available. (reserved: {billing_key.reserved_balance})",
                    "type": "insufficient_quota",
                    "code": "insufficient_balance",
                }
            },
        )

    # Check validity date
    if key.validity_date is not None:
        if time.time() > key.validity_date:
            logger.warning(
                "Key validity date expired",
                extra={
                    "key_hash": key.hashed_key[:8] + "...",
                    "validity_date": key.validity_date,
                    "current_time": time.time(),
                },
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "error": {
                        "message": "API key has expired (validity date reached).",
                        "type": "invalid_request_error",
                        "code": "key_expired",
                    }
                },
            )

    # Check balance limit for child keys (or any key with a limit)
    if key.balance_limit is not None:
        await check_and_reset_limit(key, session)

        if (
            key.total_spent + key.reserved_balance + cost_per_request
            > key.balance_limit
        ):
            logger.warning(
                "Balance limit exceeded",
                extra={
                    "key_hash": key.hashed_key[:8] + "...",
                    "total_spent": key.total_spent,
                    "reserved": key.reserved_balance,
                    "balance_limit": key.balance_limit,
                    "required": cost_per_request,
                },
            )
            raise HTTPException(
                status_code=402,
                detail={
                    "error": {
                        "message": f"Balance limit exceeded: {key.balance_limit} mSats limit. {key.total_spent} already spent ({key.reserved_balance} reserved), {cost_per_request} required for this request.",
                        "type": "insufficient_quota",
                        "code": "balance_limit_exceeded",
                    }
                },
            )

    logger.debug(
        "Charging base cost for request",
        extra={
            "key_hash": key.hashed_key[:8] + "...",
            "billing_key_hash": billing_key.hashed_key[:8] + "...",
            "cost": cost_per_request,
            "balance_before": billing_key.balance,
        },
    )

    # Create the durable reservation identity before changing aggregate balances.
    # The row and balance updates commit together, so every reserved amount has one
    # owner that can reach exactly one terminal state.
    reservation = ReservationSnapshot(
        release_id=uuid.uuid4().hex,
        key_hash=key.hashed_key,
        billing_key_hash=billing_key.hashed_key,
        reserved_msats=cost_per_request,
    )

    # Charge the base cost for the request atomically to avoid race conditions
    reserved_at_now = int(time.time())
    stmt = (
        update(ApiKey)
        .where(col(ApiKey.hashed_key) == billing_key.hashed_key)
        .where(col(ApiKey.balance) - col(ApiKey.reserved_balance) >= cost_per_request)
        .values(
            reserved_balance=col(ApiKey.reserved_balance) + cost_per_request,
            reserved_at=reserved_at_now,
            total_requests=col(ApiKey.total_requests) + 1,
        )
    )
    result = await session.exec(stmt)  # type: ignore[call-overload]

    if result.rowcount == 0:
        await session.refresh(billing_key)
        total_balance = billing_key.balance
        reserved_balance = billing_key.reserved_balance
        available_balance = max(0, total_balance - reserved_balance)
        logger.warning(
            "Concurrent request depleted available balance",
            extra={
                "key_hash": key.hashed_key[:8] + "...",
                "billing_key_hash": billing_key.hashed_key[:8] + "...",
                "required_cost": cost_per_request,
                "total_balance": total_balance,
                "reserved_balance": reserved_balance,
                "available_balance": available_balance,
            },
        )

        raise HTTPException(
            status_code=402,
            detail={
                "error": {
                    "message": f"Insufficient balance: {cost_per_request} mSats required. {available_balance} available.",
                    "type": "insufficient_quota",
                    "code": "insufficient_balance",
                    "available_balance": available_balance,
                }
            },
        )

    # Also increment total_requests and reserved_balance on the child key if it's different.
    # The balance_limit guard is enforced atomically here — the Python pre-check above
    # is a fast-path rejection only and provides no concurrency guarantee.
    if billing_key.hashed_key != key.hashed_key:
        child_stmt = (
            update(ApiKey)
            .where(col(ApiKey.hashed_key) == key.hashed_key)
            .where(
                (col(ApiKey.balance_limit).is_(None))
                | (
                    col(ApiKey.total_spent)
                    + col(ApiKey.reserved_balance)
                    + cost_per_request
                    <= col(ApiKey.balance_limit)
                )
            )
            .values(
                total_requests=col(ApiKey.total_requests) + 1,
                reserved_balance=col(ApiKey.reserved_balance) + cost_per_request,
                reserved_at=reserved_at_now,
            )
        )
        child_result = await session.exec(child_stmt)  # type: ignore[call-overload]

        if child_result.rowcount == 0:
            # Build the error before rollback expires ORM attributes.
            limit_message = (
                f"Balance limit exceeded: {key.balance_limit} mSats limit. "
                f"{key.total_spent} already spent ({key.reserved_balance} reserved), "
                f"{cost_per_request} required for this request."
            )
            # The parent reservation update already ran in this transaction.
            # Roll it back before failover code attempts to restore the previous
            # reservation; otherwise that later commit can persist both updates.
            await session.rollback()
            raise HTTPException(
                status_code=402,
                detail={
                    "error": {
                        "message": limit_message,
                        "type": "insufficient_quota",
                        "code": "balance_limit_exceeded",
                    }
                },
            )

    session.add(
        ReservationRelease(
            id=reservation.release_id,
            key_hash=reservation.key_hash,
            billing_key_hash=reservation.billing_key_hash,
            reserved_msats=reservation.reserved_msats,
            status="active",
        )
    )
    # Publish the identity before commit. If the commit succeeds but its
    # acknowledgement is interrupted, exact cleanup can still recover the
    # durable row. A definitely failed commit is harmless because every
    # terminal transition validates that row before touching balances.
    _current_reservation.set(reservation)
    try:
        await session.commit()
    except BaseException:
        # The database may have committed even if acknowledgement was cancelled
        # or the connection failed. Reconcile using a fresh transaction and the
        # exact durable identity; no upstream request has started yet.
        try:
            await session.rollback()
        except Exception:
            pass
        try:
            async with create_session() as cleanup_session:
                record = await cleanup_session.get(
                    ReservationRelease, reservation.release_id
                )
                if record is not None and record.status == "active":
                    await _transition_reservation_to_released(
                        reservation,
                        cleanup_session,
                        decrement_requests=True,
                        idempotent_success=True,
                    )
        except Exception:
            logger.exception(
                "Failed to reconcile ambiguous reservation commit",
                extra={"reservation_id": reservation.release_id},
            )
        finally:
            _clear_current_reservation(reservation)
        raise

    # The reservation is durable; keep its lease fresh for the whole request
    # lifetime (upstream header waits, non-streaming and streaming alike).
    _start_reservation_heartbeat(reservation)

    try:
        await session.refresh(billing_key)
        if billing_key.hashed_key != key.hashed_key:
            await session.refresh(key)
    except Exception:
        # The reservation transaction is already committed and durable. Logging
        # refresh failures must not make the caller treat it as unreserved.
        logger.exception(
            "Reservation committed but post-commit refresh failed",
            extra={"reservation_id": reservation.release_id},
        )

    logger.info(
        "Payment processed successfully",
        extra={
            "key_hash": key.hashed_key[:8] + "...",
            "billing_key_hash": billing_key.hashed_key[:8] + "...",
            "charged_amount": cost_per_request,
            "new_balance": billing_key.balance,
            "total_spent": billing_key.total_spent,
            "total_requests": billing_key.total_requests,
        },
    )
    payments_logger.info(
        "RESERVE",
        extra={
            "event": "reserve",
            "key_hash": key.hashed_key[:8] + "...",
            "billing_key_hash": billing_key.hashed_key[:8] + "...",
            "cost_reserved": cost_per_request,
            "balance": billing_key.balance,
            "reserved_balance": billing_key.reserved_balance,
            "total_spent": billing_key.total_spent,
        },
    )

    return reservation


async def revert_pay_for_request(
    key: ApiKey,
    session: AsyncSession,
    cost_per_request: int,
    reservation_snapshot: ReservationSnapshot | None = None,
) -> bool:
    """Revert the current request's durable reservation exactly once."""
    snapshot = reservation_snapshot or await get_reservation_snapshot(key, session)
    await _validate_reservation_snapshot(key, snapshot, session, require_active=False)
    if cost_per_request != snapshot.reserved_msats:
        return False
    return await _transition_reservation_to_released(
        snapshot,
        session,
        decrement_requests=True,
        idempotent_success=False,
    )


async def _validate_reservation_snapshot(
    key: ApiKey,
    snapshot: ReservationSnapshot,
    session: AsyncSession,
    *,
    require_active: bool = True,
) -> None:
    """Reject cross-request or forged reservation handles before any mutation."""
    state = inspect(key)
    identity = state.identity if state is not None else None
    key_hash = str(identity[0]) if identity else key.__dict__.get("hashed_key")
    if snapshot.key_hash != key_hash:
        raise RuntimeError("Billing reservation does not belong to this key")

    persisted_key = await session.get(ApiKey, snapshot.key_hash)
    if persisted_key is None:
        raise RuntimeError("Billing reservation key no longer exists")
    expected_billing_hash = persisted_key.parent_key_hash or persisted_key.hashed_key
    if snapshot.billing_key_hash != expected_billing_hash:
        raise RuntimeError("Billing reservation does not belong to this billing key")

    record = await session.get(ReservationRelease, snapshot.release_id)
    if (
        record is None
        or (require_active and record.status != "active")
        or record.key_hash != snapshot.key_hash
        or record.billing_key_hash != snapshot.billing_key_hash
        or record.reserved_msats != snapshot.reserved_msats
    ):
        raise RuntimeError("Billing reservation record does not match the request")


async def renew_reservation(
    snapshot: ReservationSnapshot, session: AsyncSession
) -> bool:
    """Push an active reservation's lease forward so the sweeper skips it.

    ``ReservationRelease.created_at`` doubles as the lease timestamp: the
    stale-reservation sweeper releases reservations whose ``created_at`` is
    older than the timeout, so a long-lived stream must renew it periodically
    or lose its reservation mid-flight (and finish uncharged, since release is
    terminal). Returns False once the reservation reached a terminal state.
    """
    result = await session.exec(  # type: ignore[call-overload]
        update(ReservationRelease)
        .where(col(ReservationRelease.id) == snapshot.release_id)
        .where(col(ReservationRelease.status) == "active")
        .values(created_at=int(time.time()))
    )
    await session.commit()
    return bool(result.rowcount == 1)


# One heartbeat task per in-flight reservation, keyed by release id. Started
# when the reservation is created and stopped when it reaches a terminal
# state, so every request path — header waits, non-streaming, streaming — is
# covered for its whole lifetime.
_reservation_heartbeats: dict[str, "asyncio.Task[None]"] = {}


def _start_reservation_heartbeat(snapshot: ReservationSnapshot) -> None:
    """Keep an in-flight reservation's lease fresh until it is finalized.

    Spawns a background task that renews the lease every third of the stale
    timeout using its own session, so requests longer than
    ``STALE_RESERVATION_TIMEOUT_SECONDS`` are not swept and finish charged.
    The task stops on its own once the reservation reaches a terminal state
    or its owning request task finishes; terminal transitions also stop it
    explicitly. Binding renewal to the owner's lifetime guarantees the sweeper
    can always recover a reservation whose request died without finalizing —
    a detached heartbeat would otherwise renew it forever and lock the funds.
    """
    interval = max(1, settings.stale_reservation_timeout_seconds // 3)
    owner = asyncio.current_task()

    async def beat() -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                if owner is None or owner.done():
                    # Request control is gone; let the lease expire so the
                    # sweeper can release the reservation if no terminal
                    # transition ever ran.
                    return
                try:
                    async with create_session() as session:
                        if not await renew_reservation(snapshot, session):
                            return
                except Exception:
                    logger.exception(
                        "Failed to renew billing reservation lease",
                        extra={"release_id": snapshot.release_id},
                    )
        finally:
            _reservation_heartbeats.pop(snapshot.release_id, None)

    _reservation_heartbeats[snapshot.release_id] = asyncio.create_task(beat())


async def _stop_reservation_heartbeat(release_id: str) -> None:
    """Cancel and await a reservation's heartbeat so no renewal overlaps
    finalization."""
    task = _reservation_heartbeats.pop(release_id, None)
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def get_reservation_snapshot(
    key: ApiKey, session: AsyncSession
) -> ReservationSnapshot:
    """Return the durable reservation created for the current request."""
    snapshot = _current_reservation.get()
    if snapshot is None:
        raise RuntimeError("No billing reservation is associated with this request")
    await _validate_reservation_snapshot(key, snapshot, session)
    return snapshot


async def _repair_corrupt_reservation(
    snapshot: ReservationSnapshot,
    session: AsyncSession,
    *,
    decrement_requests: bool,
) -> bool:
    """Terminalize a reservation without subtracting uncertain aggregates."""
    transition = (
        update(ReservationRelease)
        .where(col(ReservationRelease.id) == snapshot.release_id)
        .where(col(ReservationRelease.status) == "active")
        .where(col(ReservationRelease.key_hash) == snapshot.key_hash)
        .where(col(ReservationRelease.billing_key_hash) == snapshot.billing_key_hash)
        .where(col(ReservationRelease.reserved_msats) == snapshot.reserved_msats)
        .values(status="released")
    )
    result = await session.exec(transition)  # type: ignore[call-overload]
    if result.rowcount != 1:
        await session.rollback()
        return False

    if decrement_requests:
        for key_hash in {snapshot.billing_key_hash, snapshot.key_hash}:
            request_result = await session.exec(  # type: ignore[call-overload]
                update(ApiKey)
                .where(col(ApiKey.hashed_key) == key_hash)
                .values(
                    total_requests=case(
                        (
                            col(ApiKey.total_requests) > 0,
                            col(ApiKey.total_requests) - 1,
                        ),
                        else_=0,
                    )
                )
            )
            if request_result.rowcount != 1:
                await session.rollback()
                return False

    await session.commit()
    logger.error(
        "Released corrupt reservation without aggregate subtraction",
        extra={
            "reservation_id": snapshot.release_id,
            "billing_key_hash": snapshot.billing_key_hash[:8] + "...",
            "reserved_msats": snapshot.reserved_msats,
        },
    )
    await _stop_reservation_heartbeat(snapshot.release_id)
    _clear_current_reservation(snapshot)
    return True


async def _transition_reservation_to_released(
    snapshot: ReservationSnapshot,
    session: AsyncSession,
    *,
    decrement_requests: bool,
    idempotent_success: bool,
) -> bool:
    transition = (
        update(ReservationRelease)
        .where(col(ReservationRelease.id) == snapshot.release_id)
        .where(col(ReservationRelease.status) == "active")
        .where(col(ReservationRelease.key_hash) == snapshot.key_hash)
        .where(col(ReservationRelease.billing_key_hash) == snapshot.billing_key_hash)
        .where(col(ReservationRelease.reserved_msats) == snapshot.reserved_msats)
        .values(status="released")
    )
    transition_result = await session.exec(transition)  # type: ignore[call-overload]
    if transition_result.rowcount != 1:
        await session.rollback()
        existing = await session.get(ReservationRelease, snapshot.release_id)
        already_released = bool(
            idempotent_success
            and existing is not None
            and existing.status == "released"
            and existing.key_hash == snapshot.key_hash
            and existing.billing_key_hash == snapshot.billing_key_hash
            and existing.reserved_msats == snapshot.reserved_msats
        )
        if already_released:
            await _stop_reservation_heartbeat(snapshot.release_id)
        return already_released

    values: dict[str, object] = {
        "reserved_balance": col(ApiKey.reserved_balance) - snapshot.reserved_msats,
        "reserved_at": case(
            (
                col(ApiKey.reserved_balance) - snapshot.reserved_msats > 0,
                col(ApiKey.reserved_at),
            ),
            else_=None,
        ),
    }
    if decrement_requests:
        values["total_requests"] = col(ApiKey.total_requests) - 1

    release_stmt = (
        update(ApiKey)
        .where(col(ApiKey.hashed_key) == snapshot.billing_key_hash)
        .where(col(ApiKey.reserved_balance) >= snapshot.reserved_msats)
        .values(**values)
    )
    result = await session.exec(release_stmt)  # type: ignore[call-overload]
    if result.rowcount != 1:
        await session.rollback()
        return await _repair_corrupt_reservation(
            snapshot, session, decrement_requests=decrement_requests
        )

    if snapshot.billing_key_hash != snapshot.key_hash:
        child_release_stmt = (
            update(ApiKey)
            .where(col(ApiKey.hashed_key) == snapshot.key_hash)
            .where(col(ApiKey.reserved_balance) >= snapshot.reserved_msats)
            .values(**values)
        )
        child_result = await session.exec(  # type: ignore[call-overload]
            child_release_stmt
        )
        if child_result.rowcount != 1:
            await session.rollback()
            return await _repair_corrupt_reservation(
                snapshot, session, decrement_requests=decrement_requests
            )

    await session.commit()
    await _stop_reservation_heartbeat(snapshot.release_id)
    _clear_current_reservation(snapshot)
    return True


async def release_reservation(
    snapshot: ReservationSnapshot,
    session: AsyncSession,
    reserved_msats: int,
) -> bool:
    """Release one durable reservation exactly once without charging."""
    if reserved_msats <= 0 or reserved_msats != snapshot.reserved_msats:
        return False
    return await _transition_reservation_to_released(
        snapshot,
        session,
        decrement_requests=False,
        idempotent_success=True,
    )


async def _claim_reservation_for_charge(
    snapshot: ReservationSnapshot, session: AsyncSession
) -> bool:
    """Claim an active reservation in the caller's charge transaction."""
    statement = (
        update(ReservationRelease)
        .where(col(ReservationRelease.id) == snapshot.release_id)
        .where(col(ReservationRelease.status) == "active")
        .where(col(ReservationRelease.key_hash) == snapshot.key_hash)
        .where(col(ReservationRelease.billing_key_hash) == snapshot.billing_key_hash)
        .where(col(ReservationRelease.reserved_msats) == snapshot.reserved_msats)
        .values(status="charged")
    )
    result = await session.exec(statement)  # type: ignore[call-overload]
    if result.rowcount == 1:
        # The claim is not committed yet — the heartbeat must keep running
        # until the surrounding charge transaction commits, or a rollback
        # would restore an active reservation with no lease renewal.
        _clear_current_reservation(snapshot)
        return True

    await session.rollback()
    return False


async def _charge_reservation_rows(
    session: AsyncSession,
    *,
    billing_key_hash: str,
    key_hash: str,
    reserved_msats: int,
    charge_msats: int,
    extra_billing_guards: tuple = (),
) -> bool:
    """Release the reserved amount and record the charge on the billing row
    (and the child row when different) inside the caller's transaction.

    Guarded subtraction replaces defensive clamping: every row must still hold
    the full reserved amount, otherwise the whole transaction rolls back and
    nothing is charged. A violated invariant must never silently erase the
    aggregate reservations of sibling requests. Returns False after rollback.
    """
    billing_stmt = (
        update(ApiKey)
        .where(col(ApiKey.hashed_key) == billing_key_hash)
        .where(col(ApiKey.reserved_balance) >= reserved_msats)
        .values(
            reserved_balance=col(ApiKey.reserved_balance) - reserved_msats,
            reserved_at=case(
                (
                    col(ApiKey.reserved_balance) - reserved_msats > 0,
                    col(ApiKey.reserved_at),
                ),
                else_=None,
            ),
            balance=col(ApiKey.balance) - charge_msats,
            total_spent=col(ApiKey.total_spent) + charge_msats,
        )
    )
    for guard in extra_billing_guards:
        billing_stmt = billing_stmt.where(guard)
    result = await session.exec(billing_stmt)  # type: ignore[call-overload]
    if result.rowcount != 1:
        await session.rollback()
        return False

    if key_hash != billing_key_hash:
        child_result = await session.exec(  # type: ignore[call-overload]
            update(ApiKey)
            .where(col(ApiKey.hashed_key) == key_hash)
            .where(col(ApiKey.reserved_balance) >= reserved_msats)
            .values(
                reserved_balance=col(ApiKey.reserved_balance) - reserved_msats,
                reserved_at=case(
                    (
                        col(ApiKey.reserved_balance) - reserved_msats > 0,
                        col(ApiKey.reserved_at),
                    ),
                    else_=None,
                ),
                total_spent=col(ApiKey.total_spent) + charge_msats,
            )
        )
        if child_result.rowcount != 1:
            await session.rollback()
            return False
    return True


async def adjust_payment_for_tokens(
    key: ApiKey,
    response_data: dict,
    session: AsyncSession,
    deducted_max_cost: int,
    model_obj: "Model | None" = None,
    provider_fee: float | None = None,
    reservation_snapshot: ReservationSnapshot | None = None,
) -> dict:
    """
    Adjusts the payment based on token usage in the response.
    This is called after the initial payment and the upstream request is complete.
    Returns cost data to be included in the response.

    ``model_obj`` is the model that actually served the request; it is passed
    through to ``calculate_cost`` so billing uses the serving candidate's
    pricing instead of re-deriving it from the response's model string.

    The response's usage object is normalized with the default union parser in
    ``calculate_cost``.
    """
    billing_key = await get_billing_key(key, session)
    reservation = reservation_snapshot or await get_reservation_snapshot(key, session)
    await _validate_reservation_snapshot(
        key, reservation, session, require_active=False
    )
    # The persisted amount is authoritative if request-level minimum pricing
    # changed the caller's original estimate.
    deducted_max_cost = reservation.reserved_msats
    model = response_data.get("model", "unknown")
    # Failure paths log after a rollback has expired the ORM instances, so
    # capture the identifiers as plain strings up front.
    key_log_hash = key.hashed_key[:8] + "..."
    billing_log_hash = billing_key.hashed_key[:8] + "..."

    logger.debug(
        "Starting payment adjustment for tokens",
        extra={
            "key_hash": key.hashed_key[:8] + "...",
            "billing_key_hash": billing_key.hashed_key[:8] + "...",
            "model": model,
            "deducted_max_cost": deducted_max_cost,
            "current_balance": billing_key.balance,
            "has_usage": "usage" in response_data,
        },
    )

    async def release_reservation_only() -> None:
        """Fallback to release this request's reservation without charging."""
        try:
            released = await release_reservation(
                reservation, session, reservation.reserved_msats
            )
            logger.warning(
                "Released reservation without charging (fallback)"
                if released
                else "Reservation was already finalized; fallback skipped",
                extra={
                    "key_hash": key_log_hash,
                    "billing_key_hash": billing_log_hash,
                    "deducted_max_cost": deducted_max_cost,
                },
            )
        except Exception as e:
            logger.error(
                "Failed to release reservation in fallback",
                extra={
                    "error": str(e),
                    "key_hash": key_log_hash,
                    "billing_key_hash": billing_log_hash,
                },
            )

    async def _accumulate_fee(total_cost_msats: int) -> None:
        if total_cost_msats > 0 and ROUTSTR_FEE_PERCENT > 0:
            fee_msats = math.ceil(total_cost_msats * ROUTSTR_FEE_PERCENT / 100)
            try:
                await accumulate_routstr_fee(session, fee_msats)
            except Exception as e:
                logger.warning(
                    "Failed to accumulate Routstr fee",
                    extra={"error": str(e), "fee_msats": fee_msats},
                )

    calculated_cost = await calculate_cost(
        response_data, deducted_max_cost, model_obj, provider_fee
    )
    if not isinstance(calculated_cost, CostDataError):
        if not await _claim_reservation_for_charge(reservation, session):
            # A prior charge or release already owns this reservation. Returning
            # the calculated metadata is safe; the aggregate balances must not
            # be modified a second time.
            calculated_cost.charged_msats = 0
            return calculated_cost.dict()

    match calculated_cost:
        case MaxCostData() as cost:
            logger.debug(
                "Using max cost data (no token adjustment)",
                extra={
                    "key_hash": key.hashed_key[:8] + "...",
                    "billing_key_hash": billing_key.hashed_key[:8] + "...",
                    "model": model,
                    "max_cost": cost.total_msats,
                },
            )
            # Finalize by releasing the reservation and charging max cost.
            charged = await _charge_reservation_rows(
                session,
                billing_key_hash=billing_key.hashed_key,
                key_hash=key.hashed_key,
                reserved_msats=deducted_max_cost,
                charge_msats=cost.total_msats,
            )
            if charged:
                await session.commit()
                await _stop_reservation_heartbeat(reservation.release_id)
            if not charged:
                logger.error(
                    "Failed to finalize max-cost payment - retrying reservation release",
                    extra={
                        "key_hash": key_log_hash,
                        "billing_key_hash": billing_log_hash,
                        "deducted_max_cost": deducted_max_cost,
                        "total_cost": cost.total_msats,
                        "model": model,
                    },
                )
                cost.charged_msats = 0
                await release_reservation_only()
            else:
                cost.charged_msats = cost.total_msats
                await session.refresh(billing_key)
                if billing_key.hashed_key != key.hashed_key:
                    await session.refresh(key)
                logger.info(
                    "Max cost payment finalized",
                    extra={
                        "key_hash": key.hashed_key[:8] + "...",
                        "billing_key_hash": billing_key.hashed_key[:8] + "...",
                        "charged_amount": cost.total_msats,
                        "input_tokens": cost.input_tokens,
                        "output_tokens": cost.output_tokens,
                        "new_balance": billing_key.balance,
                        "model": model,
                    },
                )
                await _accumulate_fee(cost.total_msats)
                payments_logger.info(
                    "FINALIZE",
                    extra={
                        "event": "finalize",
                        "key_hash": key.hashed_key[:8] + "...",
                        "billing_key_hash": billing_key.hashed_key[:8] + "...",
                        "model": model,
                        "cost_reserved": deducted_max_cost,
                        "cost_charged": cost.total_msats,
                        "input_tokens": cost.input_tokens,
                        "output_tokens": cost.output_tokens,
                        "balance": billing_key.balance,
                        "reserved_balance": billing_key.reserved_balance,
                        "total_spent": billing_key.total_spent,
                        "finalize_type": "max_cost",
                    },
                )
            return cost.dict()

        case CostData() as cost:
            # If token-based pricing is enabled and base cost is 0, use token-based cost
            # Otherwise, token cost is additional to the base cost
            cost_difference = cost.total_msats - deducted_max_cost
            total_cost_msats: int = math.ceil(cost.total_msats)

            logger.info(
                "Calculated token-based cost",
                extra={
                    "key_hash": key.hashed_key[:8] + "...",
                    "billing_key_hash": billing_key.hashed_key[:8] + "...",
                    "model": model,
                    "token_cost": cost.total_msats,
                    "deducted_max_cost": deducted_max_cost,
                    "cost_difference": cost_difference,
                    "input_msats": cost.input_msats,
                    "output_msats": cost.output_msats,
                    "input_tokens": cost.input_tokens,
                    "output_tokens": cost.output_tokens,
                },
            )

            if cost_difference == 0:
                logger.debug(
                    "Finalizing with exact reserved cost",
                    extra={
                        "key_hash": key.hashed_key[:8] + "...",
                        "billing_key_hash": billing_key.hashed_key[:8] + "...",
                        "model": model,
                    },
                )
                if not await _charge_reservation_rows(
                    session,
                    billing_key_hash=billing_key.hashed_key,
                    key_hash=key.hashed_key,
                    reserved_msats=deducted_max_cost,
                    charge_msats=total_cost_msats,
                ):
                    logger.error(
                        "Failed to finalize exact-cost payment - releasing reservation",
                        extra={
                            "key_hash": key_log_hash,
                            "billing_key_hash": billing_log_hash,
                            "deducted_max_cost": deducted_max_cost,
                            "total_cost": total_cost_msats,
                            "model": model,
                        },
                    )
                    cost.charged_msats = 0
                    await release_reservation_only()
                    return cost.dict()

                await session.commit()
                await _stop_reservation_heartbeat(reservation.release_id)
                cost.charged_msats = total_cost_msats
                await session.refresh(billing_key)
                if billing_key.hashed_key != key.hashed_key:
                    await session.refresh(key)
                await _accumulate_fee(total_cost_msats)
                payments_logger.info(
                    "FINALIZE",
                    extra={
                        "event": "finalize",
                        "key_hash": key.hashed_key[:8] + "...",
                        "billing_key_hash": billing_key.hashed_key[:8] + "...",
                        "model": model,
                        "cost_reserved": deducted_max_cost,
                        "cost_charged": total_cost_msats,
                        "input_tokens": cost.input_tokens,
                        "output_tokens": cost.output_tokens,
                        "balance": billing_key.balance,
                        "reserved_balance": billing_key.reserved_balance,
                        "total_spent": billing_key.total_spent,
                        "finalize_type": "exact",
                    },
                )
                return cost.dict()

            # actual cost exceeded discounted reservation (due to tolerance_percentage)
            if cost_difference > 0:
                # Lock the billing row so the parent and child record the same
                # database-determined charge under concurrent finalizations.
                actual_charge_msats = 0
                for attempt in range(5):
                    locked_billing_key = (
                        await session.exec(
                            select(ApiKey)
                            .where(col(ApiKey.hashed_key) == billing_key.hashed_key)
                            .with_for_update()
                            .execution_options(populate_existing=True)
                        )
                    ).one()
                    observed_balance = locked_billing_key.balance
                    observed_reserved = locked_billing_key.reserved_balance
                    # An overrun may only spend this request's own reservation
                    # plus funds no other in-flight request has reserved.
                    # Charging against the raw balance would consume sibling
                    # reservations and drive the available balance negative.
                    if observed_reserved < deducted_max_cost:
                        # Invariant violated — never clamp and charge anyway,
                        # that would erase sibling reservations. Release only.
                        logger.error(
                            "reserved_balance below reservation on overrun finalization — releasing without charge",
                            extra={
                                "key_hash": key_log_hash,
                                "billing_key_hash": billing_log_hash,
                                "reserved_balance": observed_reserved,
                                "deducted_max_cost": deducted_max_cost,
                                "total_cost_msats": total_cost_msats,
                                "model": model,
                            },
                        )
                        await session.rollback()
                        cost.charged_msats = 0
                        await release_reservation_only()
                        return cost.dict()
                    sibling_reserved = observed_reserved - deducted_max_cost
                    chargeable_msats = max(0, observed_balance - sibling_reserved)
                    actual_charge_msats = min(chargeable_msats, total_cost_msats)
                    if await _charge_reservation_rows(
                        session,
                        billing_key_hash=billing_key.hashed_key,
                        key_hash=key.hashed_key,
                        reserved_msats=deducted_max_cost,
                        charge_msats=actual_charge_msats,
                        extra_billing_guards=(
                            col(ApiKey.balance) == observed_balance,
                            col(ApiKey.reserved_balance) == observed_reserved,
                        ),
                    ):
                        break
                    if not await _claim_reservation_for_charge(reservation, session):
                        cost.charged_msats = 0
                        return cost.dict()
                else:
                    await session.rollback()
                    raise RuntimeError("Could not atomically finalize cost overrun")

                await session.commit()
                await _stop_reservation_heartbeat(reservation.release_id)

                await session.refresh(billing_key)
                if billing_key.hashed_key != key.hashed_key:
                    await session.refresh(key)
                cost.charged_msats = actual_charge_msats
                if actual_charge_msats < total_cost_msats:
                    logger.warning(
                        "Cost overrun exceeded chargeable funds — shortfall written off",
                        extra={
                            "key_hash": key.hashed_key[:8] + "...",
                            "billing_key_hash": billing_key.hashed_key[:8] + "...",
                            "actual_cost_msats": total_cost_msats,
                            "charged_msats": actual_charge_msats,
                            "shortfall_msats": total_cost_msats - actual_charge_msats,
                            "model": model,
                        },
                    )
                logger.info(
                    "Finalized payment with additional charge",
                    extra={
                        "key_hash": key.hashed_key[:8] + "...",
                        "billing_key_hash": billing_key.hashed_key[:8] + "...",
                        "charged_amount": actual_charge_msats,
                        "new_balance": billing_key.balance,
                        "model": model,
                    },
                )
                await _accumulate_fee(actual_charge_msats)
                payments_logger.info(
                    "FINALIZE",
                    extra={
                        "event": "finalize",
                        "key_hash": key.hashed_key[:8] + "...",
                        "billing_key_hash": billing_key.hashed_key[:8] + "...",
                        "model": model,
                        "cost_reserved": deducted_max_cost,
                        "cost_charged": actual_charge_msats,
                        "input_tokens": cost.input_tokens,
                        "output_tokens": cost.output_tokens,
                        "balance": billing_key.balance,
                        "reserved_balance": billing_key.reserved_balance,
                        "total_spent": billing_key.total_spent,
                        "finalize_type": "overrun",
                    },
                )
            else:
                # Refund some of the base cost
                refund = abs(cost_difference)
                logger.info(
                    "Refunding excess payment",
                    extra={
                        "key_hash": key.hashed_key[:8] + "...",
                        "billing_key_hash": billing_key.hashed_key[:8] + "...",
                        "refund_amount": refund,
                        "current_balance": billing_key.balance,
                        "model": model,
                    },
                )

                charged = await _charge_reservation_rows(
                    session,
                    billing_key_hash=billing_key.hashed_key,
                    key_hash=key.hashed_key,
                    reserved_msats=deducted_max_cost,
                    charge_msats=total_cost_msats,
                )
                if charged:
                    await session.commit()
                    await _stop_reservation_heartbeat(reservation.release_id)

                if not charged:
                    logger.error(
                        "Failed to finalize payment - releasing reservation",
                        extra={
                            "key_hash": key_log_hash,
                            "billing_key_hash": billing_log_hash,
                            "deducted_max_cost": deducted_max_cost,
                            "total_cost": total_cost_msats,
                            "model": model,
                        },
                    )
                    cost.charged_msats = 0
                    await release_reservation_only()
                else:
                    cost.total_msats = total_cost_msats
                    cost.charged_msats = total_cost_msats
                    await session.refresh(billing_key)
                    if billing_key.hashed_key != key.hashed_key:
                        await session.refresh(key)

                    logger.info(
                        "Refund processed successfully",
                        extra={
                            "key_hash": key.hashed_key[:8] + "...",
                            "billing_key_hash": billing_key.hashed_key[:8] + "...",
                            "refunded_amount": refund,
                            "new_balance": billing_key.balance,
                            "final_cost": cost.total_msats,
                            "model": model,
                        },
                    )
                    await _accumulate_fee(total_cost_msats)
                    payments_logger.info(
                        "FINALIZE",
                        extra={
                            "event": "finalize",
                            "key_hash": key.hashed_key[:8] + "...",
                            "billing_key_hash": billing_key.hashed_key[:8] + "...",
                            "model": model,
                            "cost_reserved": deducted_max_cost,
                            "cost_charged": total_cost_msats,
                            "refunded": refund,
                            "input_tokens": cost.input_tokens,
                            "output_tokens": cost.output_tokens,
                            "balance": billing_key.balance,
                            "reserved_balance": billing_key.reserved_balance,
                            "total_spent": billing_key.total_spent,
                            "finalize_type": "refund",
                        },
                    )

            return cost.dict()

        case CostDataError() as error:
            logger.error(
                "Cost calculation error during payment adjustment - releasing reservation",
                extra={
                    "key_hash": key.hashed_key[:8] + "...",
                    "model": model,
                    "error_message": error.message,
                    "error_code": error.code,
                },
            )
            await release_reservation_only()

            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "message": error.message,
                        "type": "invalid_request_error",
                        "code": error.code,
                    }
                },
            )
    # All calculate_cost variants are handled above.
    raise AssertionError("Unreachable: unhandled calculate_cost result")


async def periodic_key_reset() -> None:
    """Background task to reset key limits based on their policy."""
    from .core.db import create_session

    while True:
        try:
            interval = 3600  # Run every hour
            jitter = 300
            await asyncio.sleep(interval + random.uniform(0, jitter))
        except asyncio.CancelledError:
            break

        try:
            async with create_session() as session:
                # Find all keys that have a reset policy
                stmt = select(ApiKey).where(ApiKey.balance_limit_reset.is_not(None))  # type: ignore
                keys = (await session.exec(stmt)).all()

                now = int(time.time())
                updated_count = 0

                for key in keys:
                    reset_date = key.balance_limit_reset_date or 0
                    should_reset = False

                    if key.balance_limit_reset == "daily":
                        if (
                            datetime.fromtimestamp(now).date()
                            > datetime.fromtimestamp(reset_date).date()
                        ):
                            should_reset = True
                    elif key.balance_limit_reset == "weekly":
                        if (
                            datetime.fromtimestamp(now).isocalendar()[:2]
                            > datetime.fromtimestamp(reset_date).isocalendar()[:2]
                        ):
                            should_reset = True
                    elif key.balance_limit_reset == "monthly":
                        dt_now = datetime.fromtimestamp(now)
                        dt_reset = datetime.fromtimestamp(reset_date)
                        if dt_now.year > dt_reset.year or dt_now.month > dt_reset.month:
                            should_reset = True

                    if should_reset:
                        key.total_spent = 0
                        key.balance_limit_reset_date = now
                        session.add(key)
                        updated_count += 1

                if updated_count > 0:
                    await session.commit()
                    logger.info(
                        "Periodic key reset complete",
                        extra={"keys_reset": updated_count},
                    )

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in periodic_key_reset: {e}")


async def periodic_dead_key_prune() -> None:
    """Periodically prune dead API keys. Interval <= 0 disables it.

    See ``prune_dead_api_keys`` for eligibility.
    """
    from .core.db import create_session, prune_dead_api_keys

    interval = settings.dead_key_prune_interval_seconds
    if interval <= 0:
        logger.info("Dead-key pruning disabled (interval <= 0)")
        return

    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break

        try:
            async with create_session() as session:
                await prune_dead_api_keys(session, settings.dead_key_min_age_seconds)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in periodic_dead_key_prune: {e}")


STALE_RESERVATION_SWEEP_INTERVAL_SECONDS: int = 60


async def periodic_stale_reservation_sweep() -> None:
    """Background task that releases reservations leaked by client disconnects,
    crashes or abandoned streams.
    """
    from .core.db import create_session, release_stale_reservations

    while True:
        try:
            async with create_session() as session:
                await release_stale_reservations(
                    session, settings.stale_reservation_timeout_seconds
                )
        except Exception:
            logger.exception("Error in periodic_stale_reservation_sweep")

        await asyncio.sleep(STALE_RESERVATION_SWEEP_INTERVAL_SECONDS)
