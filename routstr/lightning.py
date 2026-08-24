import asyncio
import hashlib
import re
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncGenerator

from cashu.core.base import MintQuoteState
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm.attributes import set_committed_value
from sqlmodel import col, select, update
from sqlmodel.ext.asyncio.session import AsyncSession

from .core.db import (
    INVOICE_EXPIRY_GRACE_SECONDS,
    ApiKey,
    LightningInvoice,
    create_session,
    get_session,
)
from .core.logging import get_logger
from .core.settings import settings
from .mint import (
    is_mint_rate_limited,
    mint_cooldown_remaining,
    run_mint_operation,
)
from .wallet import (
    MintConnectionError,
    get_wallet,
    is_mint_connection_error,
    wallet_operation_guard,
)

logger = get_logger(__name__)

lightning_router = APIRouter(prefix="/lightning")


# Avoid duplicate work within one process. Cross-process settlement is fenced
# by claiming a paid quote before minting and by the final conditional update.
@dataclass
class _InvoiceLockEntry:
    lock: asyncio.Lock
    users: int = 0


_invoice_settlement_locks: dict[str, _InvoiceLockEntry] = {}


@asynccontextmanager
async def _invoice_settlement_lock(invoice_id: str) -> AsyncGenerator[None, None]:
    """Serialize one invoice and remove its lock after the last waiter leaves."""

    entry = _invoice_settlement_locks.get(invoice_id)
    if entry is None:
        entry = _InvoiceLockEntry(asyncio.Lock())
        _invoice_settlement_locks[invoice_id] = entry
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users -= 1
        if entry.users == 0 and _invoice_settlement_locks.get(invoice_id) is entry:
            del _invoice_settlement_locks[invoice_id]


@dataclass(frozen=True)
class _InvoiceSettlement:
    id: str
    payment_hash: str
    amount_sats: int
    purpose: str
    api_key_hash: str | None
    mint_url: str | None
    balance_limit: int | None
    balance_limit_reset: str | None
    validity_date: int | None

    @classmethod
    def from_invoice(cls, invoice: LightningInvoice) -> "_InvoiceSettlement":
        return cls(
            id=invoice.id,
            payment_hash=invoice.payment_hash,
            amount_sats=invoice.amount_sats,
            purpose=invoice.purpose,
            api_key_hash=invoice.api_key_hash,
            mint_url=invoice.mint_url,
            balance_limit=invoice.balance_limit,
            balance_limit_reset=invoice.balance_limit_reset,
            validity_date=invoice.validity_date,
        )


def _publish_invoice_value(invoice: LightningInvoice, key: str, value: Any) -> None:
    """Update a caller view without marking a mapped object dirty."""
    try:
        set_committed_value(invoice, key, value)
    except AttributeError:
        setattr(invoice, key, value)


class InvoiceCreateRequest(BaseModel):
    amount_sats: int = Field(gt=0, le=1_000_000, description="Amount in satoshis")
    purpose: str = Field(
        default="create",
        description="create or topup",
        pattern="^(create|topup)$",
    )
    api_key: str | None = Field(
        default=None,
        description="Deprecated: legacy field for topup. Prefer Authorization header.",
    )
    balance_limit: int | None = Field(default=None)
    balance_limit_reset: str | None = Field(default=None)
    validity_date: int | None = Field(default=None)


def _extract_bearer_api_key(authorization: str | None) -> str | None:
    if not authorization:
        return None
    token = authorization.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token or None


class InvoiceCreateResponse(BaseModel):
    invoice_id: str
    bolt11: str
    amount_sats: int
    expires_at: int
    payment_hash: str


class InvoiceStatusResponse(BaseModel):
    status: str
    api_key: str | None = None
    amount_sats: int
    paid_at: int | None = None
    created_at: int
    expires_at: int


_SETTLEABLE_INVOICE_STATUSES = ("pending", "settlement_pending", "expired")


def _within_settlement_window(invoice: LightningInvoice, now: int) -> bool:
    if invoice.status not in _SETTLEABLE_INVOICE_STATUSES:
        return False
    if invoice.status != "expired":
        return True
    return now < invoice.expires_at + INVOICE_EXPIRY_GRACE_SECONDS


class InvoiceRecoverRequest(BaseModel):
    bolt11: str = Field(description="BOLT11 invoice string")


def _trusted_mint_candidates() -> list[str]:
    return [
        mint
        for mint in dict.fromkeys([settings.primary_mint, *settings.cashu_mints])
        if mint
    ]


async def _request_mint_with_fallback(
    amount_sats: int,
    *,
    allowed_mints: list[str] | None = None,
) -> tuple[str, str, str]:
    """Request a quote, falling back only among the allowed trusted mints.

    Guards against amount_sats <= 0: the cashu library's PostMintQuoteRequest
    enforces ``amount > 0`` (Pydantic Field(gt=0)), so passing 0 raises a
    cryptic validation error deep in the stack.  Fail fast with context.
    """
    if amount_sats <= 0:
        raise ValueError(
            f"generate_lightning_invoice: amount_sats must be > 0, got {amount_sats}."
        )
    tried: list[str] = []
    trusted = _trusted_mint_candidates()
    if allowed_mints:
        # Persisted mint preferences (e.g. an API key's refund_mint_url) must
        # not outlive the operator's trusted-mint configuration.
        candidates = [m for m in dict.fromkeys(allowed_mints) if m in trusted]
        if not candidates:
            logger.warning(
                "Requested mints are no longer trusted; falling back to "
                "configured mints",
                extra={
                    "requested_mints": list(dict.fromkeys(allowed_mints)),
                    "op_name": "request_mint_invoice",
                },
            )
            candidates = trusted
    else:
        candidates = trusted
    for mint_url in candidates:
        cooldown = mint_cooldown_remaining(mint_url)
        if cooldown > 0:
            tried.append(f"{mint_url}: cooling down")
            logger.info(
                "Skipping mint during cooldown",
                extra={
                    "mint_url": mint_url,
                    "cooldown_seconds": round(cooldown, 2),
                    "op_name": "request_mint_invoice",
                },
            )
            continue
        try:
            wallet = await get_wallet(mint_url, "sat", retry_on_rate_limit=False)
            quote = await run_mint_operation(
                lambda: wallet.request_mint(amount_sats),
                op_name="request_mint_invoice",
                mint_url=mint_url,
                retry_on_rate_limit=False,
            )
            return quote.request, quote.quote, mint_url
        except Exception as e:
            tried.append(f"{mint_url}: {type(e).__name__}")
            if not is_mint_connection_error(e) and not is_mint_rate_limited(e):
                raise
            logger.warning(
                "request_mint failed, trying fallback mint",
                extra={
                    "failed_mint": mint_url,
                    "error": str(e),
                    "tried": tried,
                },
            )
            continue
    raise MintConnectionError(f"All mints failed for request_mint: {tried}")


async def generate_lightning_invoice(
    amount_sats: int,
    description: str,
    *,
    allowed_mints: list[str] | None = None,
) -> tuple[str, str, str]:
    bolt11, payment_hash, mint_url = await _request_mint_with_fallback(
        amount_sats, allowed_mints=allowed_mints
    )
    return bolt11, payment_hash, mint_url


def generate_invoice_id() -> str:
    return secrets.token_urlsafe(16)


@lightning_router.post("/invoice", response_model=InvoiceCreateResponse)
async def create_invoice(
    request: InvoiceCreateRequest,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> InvoiceCreateResponse:
    api_key_token = _extract_bearer_api_key(authorization) or request.api_key
    topup_api_key: ApiKey | None = None

    if request.purpose == "topup":
        if not api_key_token:
            raise HTTPException(
                status_code=401,
                detail="Authorization bearer api key is required for topup",
            )
        if not api_key_token.startswith("sk-"):
            raise HTTPException(status_code=400, detail="Invalid API key format")

        topup_api_key = await session.get(ApiKey, api_key_token[3:])
        if not topup_api_key:
            raise HTTPException(status_code=404, detail="API key not found")

    try:
        description = f"Routstr {request.purpose} {request.amount_sats} sats"
        allowed_mints = None
        if request.purpose == "topup":
            assert topup_api_key is not None
            # A key's liabilities are attributed to a single refund mint. Keep
            # top-up collateral on that same mint so balances and payouts cannot
            # misclassify funds held by another mint as owner profit.
            allowed_mints = [topup_api_key.refund_mint_url or settings.primary_mint]
        bolt11, payment_hash, mint_url = await generate_lightning_invoice(
            request.amount_sats, description, allowed_mints=allowed_mints
        )

        invoice_id = generate_invoice_id()
        expires_at = int(time.time()) + 3600  # 1 hour expiry

        invoice = LightningInvoice(
            id=invoice_id,
            bolt11=bolt11,
            amount_sats=request.amount_sats,
            description=description,
            payment_hash=payment_hash,
            status="pending",
            api_key_hash=api_key_token[3:] if api_key_token else None,
            purpose=request.purpose,
            mint_url=mint_url,
            balance_limit=request.balance_limit,
            balance_limit_reset=request.balance_limit_reset,
            validity_date=request.validity_date,
            expires_at=expires_at,
        )

        session.add(invoice)
        await session.commit()

        logger.info(
            "Lightning invoice created",
            extra={
                "invoice_id": invoice_id,
                "amount_sats": request.amount_sats,
                "purpose": request.purpose,
                "expires_at": expires_at,
            },
        )

        return InvoiceCreateResponse(
            invoice_id=invoice_id,
            bolt11=bolt11,
            amount_sats=request.amount_sats,
            expires_at=expires_at,
            payment_hash=payment_hash,
        )

    except Exception as e:
        logger.error(f"Failed to create Lightning invoice: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to create Lightning invoice"
        )


@lightning_router.get(
    "/invoice/{invoice_id}/status", response_model=InvoiceStatusResponse
)
async def get_invoice_status(
    invoice_id: str,
    session: AsyncSession = Depends(get_session),
) -> InvoiceStatusResponse:
    invoice = await session.get(LightningInvoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    definitively_unpaid = False
    if _within_settlement_window(invoice, int(time.time())):
        definitively_unpaid = await check_invoice_payment(invoice, session)
    await _expire_invoice_if_authoritatively_unpaid(
        invoice, session, definitively_unpaid
    )

    api_key = None
    if invoice.status == "paid" and invoice.purpose == "create":
        if invoice.api_key_hash:
            api_key = f"sk-{invoice.api_key_hash}"
    elif (
        invoice.status == "paid" and invoice.purpose == "topup" and invoice.api_key_hash
    ):
        api_key = f"sk-{invoice.api_key_hash}"

    return InvoiceStatusResponse(
        status=invoice.status,
        api_key=api_key,
        amount_sats=invoice.amount_sats,
        paid_at=invoice.paid_at,
        created_at=invoice.created_at,
        expires_at=invoice.expires_at,
    )


@lightning_router.post("/recover", response_model=InvoiceStatusResponse)
async def recover_invoice(
    request: InvoiceRecoverRequest,
    session: AsyncSession = Depends(get_session),
) -> InvoiceStatusResponse:
    result = await session.exec(
        select(LightningInvoice).where(LightningInvoice.bolt11 == request.bolt11)
    )
    invoice = result.first()

    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # Recovery is the last remedy for a payment we never observed, so it ignores
    # the grace window. Holding the bolt11 already proves the caller owns it.
    definitively_unpaid = False
    if invoice.status in _SETTLEABLE_INVOICE_STATUSES:
        definitively_unpaid = await check_invoice_payment(invoice, session)
    await _expire_invoice_if_authoritatively_unpaid(
        invoice, session, definitively_unpaid
    )

    api_key = None
    if invoice.status == "paid":
        if invoice.purpose == "create" and invoice.api_key_hash:
            api_key = f"sk-{invoice.api_key_hash}"
        elif invoice.purpose == "topup" and invoice.api_key_hash:
            api_key = f"sk-{invoice.api_key_hash}"

    return InvoiceStatusResponse(
        status=invoice.status,
        api_key=api_key,
        amount_sats=invoice.amount_sats,
        paid_at=invoice.paid_at,
        created_at=invoice.created_at,
        expires_at=invoice.expires_at,
    )


async def _claim_paid_invoice_for_settlement(
    invoice: LightningInvoice,
    caller_session: AsyncSession,
    observed_status: str,
) -> bool:
    """Claim an authoritative paid quote before consuming it at the mint."""
    if observed_status == "settlement_pending":
        return True
    if observed_status not in ("pending", "expired"):
        await _reload_invoice_view(invoice, caller_session)
        return False

    async with create_session() as claim_session:
        claim = await claim_session.exec(  # type: ignore[call-overload]
            update(LightningInvoice)
            .where(
                col(LightningInvoice.id) == invoice.id,
                col(LightningInvoice.status).in_(("pending", "expired")),
            )
            .values(status="settlement_pending")
            .execution_options(synchronize_session=False)
        )
        await claim_session.commit()

    if claim.rowcount != 1:
        await _reload_invoice_view(invoice, caller_session)
        return False

    _publish_invoice_value(invoice, "status", "settlement_pending")
    return True


async def check_invoice_payment(
    invoice: LightningInvoice, session: AsyncSession
) -> bool:
    """Settle an invoice and report whether its quote is definitively unpaid.

    False covers paid, pending, and ambiguous transport/DB outcomes so callers
    never expire a quote merely because reconciliation could not complete.
    """
    async with _invoice_settlement_lock(invoice.id), wallet_operation_guard():
        minted = False
        payment_confirmed = False
        try:
            # Snapshot the row and end the caller's read transaction before any
            # potentially slow mint I/O. All final DB mutations use owned,
            # short-lived sessions below.
            await session.refresh(invoice)
            if invoice.status not in _SETTLEABLE_INVOICE_STATUSES:
                await session.commit()
                return False
            observed_status = invoice.status
            settlement = _InvoiceSettlement.from_invoice(invoice)
            await session.commit()

            mint_url = settlement.mint_url or settings.primary_mint
            wallet = await get_wallet(mint_url, "sat", load=False)
            try:
                mint_status = await run_mint_operation(
                    lambda: wallet.get_mint_quote(settlement.payment_hash),
                    op_name="get_mint_quote",
                    mint_url=mint_url,
                )
            except Exception as error:
                if not _is_quote_not_found(error):
                    raise
                logger.info(
                    "Invoice quote no longer exists at mint, treating as unpaid",
                    extra={"invoice_id": invoice.id, "error": str(error)},
                )
                return True
            if not mint_status.paid:
                return getattr(mint_status, "state", None) == MintQuoteState.unpaid
            payment_confirmed = True

            # Fence expiry and other workers before consuming the paid quote.
            # If a concurrent expiry/finalization won, this worker must not mint.
            if not await _claim_paid_invoice_for_settlement(
                invoice, session, observed_status
            ):
                return False

            # Reject a paid top-up whose target was pruned before redeeming its
            # single-use quote. The validation session is closed before mint I/O.
            if settlement.purpose == "topup":
                if not settlement.api_key_hash:
                    raise ValueError("No API key associated with topup invoice")
                async with create_session() as validation_session:
                    target = await validation_session.get(
                        ApiKey, settlement.api_key_hash
                    )
                    if target is None:
                        terminal = await validation_session.exec(  # type: ignore[call-overload]
                            update(LightningInvoice)
                            .where(
                                col(LightningInvoice.id) == settlement.id,
                                col(LightningInvoice.status).in_(
                                    _SETTLEABLE_INVOICE_STATUSES
                                ),
                            )
                            .values(status="reconciliation_required")
                        )
                        await validation_session.commit()
                        if terminal.rowcount == 1:
                            _publish_invoice_value(
                                invoice, "status", "reconciliation_required"
                            )
                        else:
                            await _reload_invoice_view(invoice, session)
                        logger.critical(
                            "Paid topup invoice target API key was not found; reconciliation required",
                            extra={"invoice_id": settlement.id},
                        )
                        return False

            # Quote-linked proof verification makes an ambiguous mint response
            # retryable without crediting unrelated wallet balance growth.
            wallet = await get_wallet(mint_url, "sat")
            await _mint_invoice_quote(wallet, settlement)
            minted = True

            paid_at = int(time.time())
            async with create_session() as finalization_session:
                settled, api_key_hash = await _finalize_invoice_settlement(
                    settlement, finalization_session, paid_at
                )
            if not settled:
                await _reload_invoice_view(invoice, session)
                return False

            _publish_invoice_value(invoice, "status", "paid")
            _publish_invoice_value(invoice, "paid_at", paid_at)
            _publish_invoice_value(invoice, "api_key_hash", api_key_hash)
            logger.info(
                "Lightning invoice paid",
                extra={
                    "invoice_id": settlement.id,
                    "amount_sats": settlement.amount_sats,
                    "purpose": settlement.purpose,
                    "api_key_hash": api_key_hash[:8] + "..." if api_key_hash else None,
                },
            )
            return False
        except BaseException as error:
            # Never roll back the caller-owned session: doing so expires invoice
            # and sibling ORM objects. Owned sessions roll themselves back.
            if payment_confirmed and invoice.status != "settlement_pending":
                try:
                    async with create_session() as state_session:
                        pending = await state_session.exec(  # type: ignore[call-overload]
                            update(LightningInvoice)
                            .where(
                                col(LightningInvoice.id) == invoice.id,
                                col(LightningInvoice.status).in_(
                                    _SETTLEABLE_INVOICE_STATUSES
                                ),
                            )
                            .values(status="settlement_pending")
                        )
                        await state_session.commit()
                    if pending.rowcount == 1:
                        _publish_invoice_value(invoice, "status", "settlement_pending")
                except Exception as state_error:
                    logger.critical(
                        "Paid invoice reconciliation state could not be persisted",
                        extra={"invoice_id": invoice.id, "error": str(state_error)},
                    )
            if minted:
                logger.critical(
                    "Invoice mint succeeded but DB finalization failed; reconciliation required",
                    extra={"invoice_id": invoice.id, "purpose": invoice.purpose},
                )
            try:
                await _reload_invoice_view(invoice, session)
            except Exception:
                pass
            if not isinstance(error, Exception):
                raise
            logger.error(f"Failed to check invoice payment: {error}")
            return False


def _is_quote_not_found(error: BaseException) -> bool:
    """Check if the error indicates the mint no longer has this quote.

    ``Unknown quote`` is definitive on wording alone and its numeric code varies
    between mints. ``quote not found`` needs ``Code: 0`` because some mints reuse
    that wording for ambiguous states.
    """
    message = str(error)
    if re.search(r"\bunknown\s+quote\b", message, re.IGNORECASE):
        return True
    return bool(
        re.search(r"\bquote\s+not\s+found\b", message, re.IGNORECASE)
        and re.search(r"\bcode\s*:?\s*0\b", message, re.IGNORECASE)
    )


def _is_outputs_already_signed(error: BaseException) -> bool:
    message = str(error)
    return bool(
        re.search(
            r"\boutputs?\s+(?:have\s+)?already\s+(?:been\s+)?signed(?:\s+before)?\b",
            message,
            re.IGNORECASE,
        )
        and re.search(r"\bcode\s*:\s*11003\b", message, re.IGNORECASE)
    )


def _invoice_quote_proof_amount(wallet: Any, quote_id: str) -> int:
    """Return spendable wallet value minted by one Lightning quote."""
    return sum(
        proof.amount
        for proof in wallet.proofs
        if proof.mint_id == quote_id and not proof.reserved
    )


async def _mint_invoice_quote(
    wallet: Any, invoice: LightningInvoice | _InvoiceSettlement
) -> None:
    """Mint a paid quote, proving quote-linked outputs before DB credit."""
    mint_url = invoice.mint_url or settings.primary_mint
    await wallet.load_proofs(reload=True)
    if _invoice_quote_proof_amount(wallet, invoice.payment_hash) >= invoice.amount_sats:
        return

    try:
        await run_mint_operation(
            lambda: wallet.mint(invoice.amount_sats, quote_id=invoice.payment_hash),
            op_name=f"invoice_mint_{invoice.purpose}",
            mint_url=mint_url,
            retry_timeouts=False,
        )
    except Exception as error:
        if not _is_outputs_already_signed(error):
            raise

        for keyset_id in wallet.keysets:
            await wallet.restore_tokens_for_keyset(keyset_id, to=1, batch=25)
        await wallet.load_proofs(reload=True)
        recovered = _invoice_quote_proof_amount(wallet, invoice.payment_hash)
        if recovered < invoice.amount_sats:
            raise RuntimeError(
                "Invoice outputs were already signed but quote-linked recovery returned "
                f"{recovered} sats; expected at least {invoice.amount_sats}"
            ) from error
    else:
        await wallet.load_proofs(reload=True)
        minted_amount = _invoice_quote_proof_amount(wallet, invoice.payment_hash)
        if minted_amount < invoice.amount_sats:
            raise RuntimeError(
                "Invoice mint succeeded but quote-linked proofs total "
                f"{minted_amount} sats; expected at least {invoice.amount_sats}"
            )


def _invoice_api_key_hash(invoice: LightningInvoice | _InvoiceSettlement) -> str:
    dummy_token = f"invoice-{invoice.id}-{invoice.payment_hash}"
    return hashlib.sha256(dummy_token.encode()).hexdigest()


async def _create_api_key_record(
    invoice: LightningInvoice | _InvoiceSettlement, session: AsyncSession
) -> ApiKey:
    mint_url = invoice.mint_url or settings.primary_mint
    api_key = ApiKey(
        hashed_key=_invoice_api_key_hash(invoice),
        balance=invoice.amount_sats * 1000,
        refund_currency="sat",
        refund_mint_url=mint_url,
        balance_limit=invoice.balance_limit,
        balance_limit_reset=invoice.balance_limit_reset,
        validity_date=invoice.validity_date,
    )
    session.add(api_key)
    await session.flush()
    return api_key


async def _topup_api_key_record(
    invoice: LightningInvoice | _InvoiceSettlement, session: AsyncSession
) -> None:
    if not invoice.api_key_hash:
        raise ValueError("No API key associated with topup invoice")
    result = await session.exec(  # type: ignore[call-overload]
        update(ApiKey)
        .where(col(ApiKey.hashed_key) == invoice.api_key_hash)
        .values(balance=col(ApiKey.balance) + invoice.amount_sats * 1000)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise ValueError("Associated API key not found")


async def _finalize_invoice_settlement(
    invoice: _InvoiceSettlement, session: AsyncSession, paid_at: int
) -> tuple[bool, str | None]:
    """Atomically fence and apply one invoice credit in the provided owned session."""
    api_key_hash = (
        _invoice_api_key_hash(invoice)
        if invoice.purpose == "create"
        else invoice.api_key_hash
    )
    claim = await session.exec(  # type: ignore[call-overload]
        update(LightningInvoice)
        .where(col(LightningInvoice.id) == invoice.id)
        .where(col(LightningInvoice.status).in_(_SETTLEABLE_INVOICE_STATUSES))
        .values(status="paid", paid_at=paid_at, api_key_hash=api_key_hash)
        .execution_options(synchronize_session=False)
    )
    if claim.rowcount != 1:
        await session.rollback()
        return False, None

    if invoice.purpose == "create":
        await _create_api_key_record(invoice, session)
    elif invoice.purpose == "topup":
        await _topup_api_key_record(invoice, session)
    else:
        raise ValueError(f"Unsupported invoice purpose: {invoice.purpose}")
    await session.commit()
    return True, api_key_hash


async def _reload_invoice_view(
    invoice: LightningInvoice, _caller_session: AsyncSession
) -> None:
    """Publish committed invoice state without touching the caller transaction."""
    async with create_session() as reload_session:
        stored = await reload_session.get(LightningInvoice, invoice.id)
        if stored is None:
            return
        status = stored.status
        paid_at = stored.paid_at
        api_key_hash = stored.api_key_hash
        await reload_session.commit()
    _publish_invoice_value(invoice, "status", status)
    _publish_invoice_value(invoice, "paid_at", paid_at)
    _publish_invoice_value(invoice, "api_key_hash", api_key_hash)


async def _expire_invoice_if_authoritatively_unpaid(
    invoice: LightningInvoice,
    caller_session: AsyncSession,
    definitively_unpaid: bool,
) -> bool:
    """Expire one overdue unpaid invoice without overwriting concurrent settlement."""
    if (
        not definitively_unpaid
        or invoice.status != "pending"
        or int(time.time()) <= invoice.expires_at
    ):
        return False

    async with create_session() as expiry_session:
        expired = await expiry_session.exec(  # type: ignore[call-overload]
            update(LightningInvoice)
            .where(
                col(LightningInvoice.id) == invoice.id,
                col(LightningInvoice.status) == "pending",
            )
            .values(status="expired")
            .execution_options(synchronize_session=False)
        )
        await expiry_session.commit()

    if expired.rowcount == 1:
        _publish_invoice_value(invoice, "status", "expired")
        return True

    await _reload_invoice_view(invoice, caller_session)
    return False


async def _credit_topup_record(
    invoice: LightningInvoice | _InvoiceSettlement, session: AsyncSession
) -> None:
    await _topup_api_key_record(invoice, session)


# Nutshell mints throttle Lightning backend lookups to once per 10s per
# quote, so polling faster just burns the global request budget for nothing.
INVOICE_WATCH_INTERVAL_SECONDS = 10
INVOICE_WATCH_BATCH_LIMIT = 100
INVOICE_WATCH_CANDIDATE_LIMIT = 500
INVOICE_POLL_MAX_INTERVAL_SECONDS = 600
SETTLEMENT_POLL_MAX_INTERVAL_SECONDS = 60


def _invoice_poll_interval(age_seconds: int) -> int:
    """Older quotes rarely settle, and the mint request budget is per IP."""
    if age_seconds < 60:
        return 10
    if age_seconds < 300:
        return 30
    if age_seconds < 1800:
        return 120
    return INVOICE_POLL_MAX_INTERVAL_SECONDS


def _invoice_poll_due(
    invoice: LightningInvoice, now: int, prev_now: int, max_interval: int
) -> bool:
    """Whether the invoice's backoff interval elapsed between the two cycles."""
    if invoice.created_at > prev_now:
        return True
    age = now - invoice.created_at
    prev_age = prev_now - invoice.created_at
    interval = min(_invoice_poll_interval(age), max_interval)
    return age // interval != prev_age // interval


async def _expire_overdue_invoices(now: int) -> int:
    """A rate-limited mint must not stall expiry."""
    async with create_session() as expiry_session:
        expired = await expiry_session.exec(  # type: ignore[call-overload]
            update(LightningInvoice)
            .where(
                col(LightningInvoice.status) == "pending",
                col(LightningInvoice.expires_at) < now,
            )
            .values(status="expired")
            .execution_options(synchronize_session=False)
        )
        await expiry_session.commit()
    return int(expired.rowcount)


async def _process_invoice_watch_batch(session: AsyncSession, prev_now: int) -> int:
    now = int(time.time())
    swept = await _expire_overdue_invoices(now)
    if swept:
        logger.info("Expired overdue invoices", extra={"invoice_count": swept})
    settling = await session.exec(
        select(LightningInvoice)
        .where(col(LightningInvoice.status) == "settlement_pending")
        .order_by(col(LightningInvoice.created_at))
        .limit(INVOICE_WATCH_BATCH_LIMIT // 2)
    )
    unpaid = await session.exec(
        select(LightningInvoice)
        .where(
            col(LightningInvoice.status) == "pending",
            col(LightningInvoice.expires_at) >= now,
        )
        .order_by(col(LightningInvoice.created_at).desc())
        .limit(INVOICE_WATCH_CANDIDATE_LIMIT)
    )
    recoverable = await session.exec(
        select(LightningInvoice)
        .where(
            col(LightningInvoice.status) == "expired",
            col(LightningInvoice.expires_at) > now - INVOICE_EXPIRY_GRACE_SECONDS,
        )
        .order_by(col(LightningInvoice.expires_at).desc())
        .limit(INVOICE_WATCH_CANDIDATE_LIMIT)
    )
    # The tail only ever consumes budget the first two groups left unused.
    due = [
        inv
        for inv in settling.all()
        if _invoice_poll_due(inv, now, prev_now, SETTLEMENT_POLL_MAX_INTERVAL_SECONDS)
    ]
    due += [
        inv
        for inv in unpaid.all()
        if _invoice_poll_due(inv, now, prev_now, INVOICE_POLL_MAX_INTERVAL_SECONDS)
    ]
    due += [
        inv
        for inv in recoverable.all()
        if _invoice_poll_due(inv, now, prev_now, INVOICE_POLL_MAX_INTERVAL_SECONDS)
    ]
    for invoice in due[:INVOICE_WATCH_BATCH_LIMIT]:
        try:
            await check_invoice_payment(invoice, session)
        except Exception as e:
            logger.error(
                "Invoice watcher failed for invoice",
                extra={"invoice_id": invoice.id, "error": str(e)},
            )
    return now


async def periodic_invoice_watcher() -> None:
    """Background task: detect paid Lightning invoices and credit balances."""
    prev_now = int(time.time()) - INVOICE_WATCH_INTERVAL_SECONDS
    while True:
        try:
            async with create_session() as session:
                prev_now = await _process_invoice_watch_batch(session, prev_now)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Invoice watcher loop error: {e}")

        await asyncio.sleep(INVOICE_WATCH_INTERVAL_SECONDS)
