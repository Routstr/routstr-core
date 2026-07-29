import asyncio
import hashlib
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm.attributes import set_committed_value
from sqlmodel import col, select, update
from sqlmodel.ext.asyncio.session import AsyncSession

from .core.db import ApiKey, LightningInvoice, create_session, get_session
from .core.logging import get_logger
from .core.settings import settings
from .wallet import (
    MintConnectionError,
    _is_mint_rate_limited,
    _mint_cooldown_remaining,
    _mint_operation,
    get_wallet,
    is_mint_connection_error,
)

logger = get_logger(__name__)

lightning_router = APIRouter(prefix="/lightning")

# Avoid duplicate work within one process. Cross-process credit fencing is done
# by the conditional pending -> paid update in _finalize_invoice_settlement().
_invoice_settlement_locks: dict[str, asyncio.Lock] = {}


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
    configured = allowed_mints or [settings.primary_mint, *settings.cashu_mints]
    candidates = list(dict.fromkeys(configured))
    for mint_url in candidates:
        cooldown = _mint_cooldown_remaining(mint_url)
        if cooldown > 0:
            tried.append(f"{mint_url}: cooling down")
            logger.info(
                "Skipping rate-limited mint",
                extra={
                    "mint_url": mint_url,
                    "cooldown_seconds": round(cooldown, 2),
                    "op_name": "request_mint_invoice",
                },
            )
            continue
        try:
            wallet = await get_wallet(mint_url, "sat", retry_on_rate_limit=False)
            quote = await _mint_operation(
                lambda: wallet.request_mint(amount_sats),
                op_name="request_mint_invoice",
                mint_url=mint_url,
                retry_on_rate_limit=False,
            )
            return quote.request, quote.quote, mint_url
        except Exception as e:
            tried.append(f"{mint_url}: {type(e).__name__}")
            if not is_mint_connection_error(e) and not _is_mint_rate_limited(e):
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
            allowed_mints = [
                topup_api_key.refund_mint_url or settings.primary_mint
            ]
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

    if invoice.status == "pending":
        await check_invoice_payment(invoice, session)

    if invoice.status == "pending" and int(time.time()) > invoice.expires_at:
        invoice.status = "expired"
        await session.commit()

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

    if invoice.status == "pending":
        await check_invoice_payment(invoice, session)

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


async def check_invoice_payment(
    invoice: LightningInvoice, session: AsyncSession
) -> None:
    lock = _invoice_settlement_locks.setdefault(invoice.id, asyncio.Lock())
    async with lock:
        minted = False
        try:
            # Snapshot the row and end the caller's read transaction before any
            # potentially slow mint I/O. All final DB mutations use owned,
            # short-lived sessions below.
            await session.refresh(invoice)
            if invoice.status != "pending":
                await session.commit()
                return
            settlement = _InvoiceSettlement.from_invoice(invoice)
            await session.commit()

            mint_url = settlement.mint_url or settings.primary_mint
            wallet = await get_wallet(mint_url, "sat")
            mint_status = await _mint_operation(
                lambda: wallet.get_mint_quote(settlement.payment_hash),
                op_name="get_mint_quote",
                mint_url=mint_url,
            )
            if not mint_status.paid:
                return

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
                                col(LightningInvoice.status) == "pending",
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
                        return

            # Quote-linked proof verification makes an ambiguous mint response
            # retryable without crediting unrelated wallet balance growth.
            await _mint_invoice_quote(wallet, settlement)
            minted = True

            paid_at = int(time.time())
            async with create_session() as finalization_session:
                settled, api_key_hash = await _finalize_invoice_settlement(
                    settlement, finalization_session, paid_at
                )
            if not settled:
                await _reload_invoice_view(invoice, session)
                return

            _publish_invoice_value(invoice, "status", "paid")
            _publish_invoice_value(invoice, "paid_at", paid_at)
            _publish_invoice_value(invoice, "api_key_hash", api_key_hash)
            logger.info(
                "Lightning invoice paid",
                extra={
                    "invoice_id": settlement.id,
                    "amount_sats": settlement.amount_sats,
                    "purpose": settlement.purpose,
                    "api_key_hash": api_key_hash[:8] + "..."
                    if api_key_hash
                    else None,
                },
            )
        except BaseException as error:
            # Never roll back the caller-owned session: doing so expires invoice
            # and sibling ORM objects. Owned sessions roll themselves back.
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
        await _mint_operation(
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
        .where(col(LightningInvoice.status) == "pending")
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


async def _credit_topup_record(
    invoice: LightningInvoice | _InvoiceSettlement, session: AsyncSession
) -> None:
    await _topup_api_key_record(invoice, session)


# Nutshell mints throttle Lightning backend lookups to once per 10s per
# quote, so polling faster just burns the global request budget for nothing.
INVOICE_WATCH_INTERVAL_SECONDS = 10
INVOICE_WATCH_BATCH_LIMIT = 100


async def periodic_invoice_watcher() -> None:
    """Background task: detect paid Lightning invoices and credit balances.

    Removes the need for clients to poll the status endpoint after paying.
    """
    while True:
        try:
            async with create_session() as session:
                now = int(time.time())
                result = await session.exec(
                    select(LightningInvoice)
                    .where(
                        LightningInvoice.status == "pending",
                        col(LightningInvoice.expires_at) > now,
                    )
                    .limit(INVOICE_WATCH_BATCH_LIMIT)
                )
                pending = result.all()
                for invoice in pending:
                    try:
                        await check_invoice_payment(invoice, session)
                    except Exception as e:
                        logger.error(
                            "Invoice watcher failed for invoice",
                            extra={"invoice_id": invoice.id, "error": str(e)},
                        )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Invoice watcher loop error: {e}")

        await asyncio.sleep(INVOICE_WATCH_INTERVAL_SECONDS)
