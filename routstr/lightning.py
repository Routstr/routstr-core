import asyncio
import hashlib
import secrets
import time

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from .core.db import ApiKey, LightningInvoice, create_session, get_session
from .core.logging import get_logger
from .core.settings import settings
from .wallet import (
    MintConnectionError,
    _is_mint_rate_limited,
    _mint_operation,
    get_wallet,
    is_mint_connection_error,
)

logger = get_logger(__name__)

lightning_router = APIRouter(prefix="/lightning")


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


async def _request_mint_with_fallback(
    amount_sats: int,
) -> tuple[str, str, str]:
    """Primary first, fall back to other trusted mints on rate-limit/transport failure."""
    tried: list[str] = []
    candidates = [settings.primary_mint] + [
        m for m in settings.cashu_mints if m != settings.primary_mint
    ]
    for mint_url in candidates:
        try:
            wallet = await get_wallet(mint_url, "sat")
            quote = await _mint_operation(
                lambda: wallet.request_mint(amount_sats),
                op_name="request_mint_invoice",
                mint_url=mint_url,
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
    amount_sats: int, description: str
) -> tuple[str, str, str]:
    bolt11, payment_hash, mint_url = await _request_mint_with_fallback(amount_sats)
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

    if request.purpose == "topup":
        if not api_key_token:
            raise HTTPException(
                status_code=401,
                detail="Authorization bearer api key is required for topup",
            )
        if not api_key_token.startswith("sk-"):
            raise HTTPException(status_code=400, detail="Invalid API key format")

        api_key = await session.get(ApiKey, api_key_token[3:])
        if not api_key:
            raise HTTPException(status_code=404, detail="API key not found")

    try:
        description = f"Routstr {request.purpose} {request.amount_sats} sats"
        bolt11, payment_hash, mint_url = await generate_lightning_invoice(
            request.amount_sats, description
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
    try:
        mint_url = invoice.mint_url or settings.primary_mint
        wallet = await get_wallet(mint_url, "sat")

        mint_status = await _mint_operation(
            lambda: wallet.get_mint_quote(invoice.payment_hash),
            op_name="get_mint_quote",
            mint_url=mint_url,
        )

        if mint_status.paid:
            invoice.status = "paid"
            invoice.paid_at = int(time.time())

            if invoice.purpose == "create":
                api_key = await create_api_key_from_invoice(invoice, session)
                invoice.api_key_hash = api_key.hashed_key
            elif invoice.purpose == "topup" and invoice.api_key_hash:
                await topup_api_key_from_invoice(invoice, session)

            await session.commit()

            logger.info(
                "Lightning invoice paid",
                extra={
                    "invoice_id": invoice.id,
                    "amount_sats": invoice.amount_sats,
                    "purpose": invoice.purpose,
                    "api_key_hash": invoice.api_key_hash[:8] + "..."
                    if invoice.api_key_hash
                    else None,
                },
            )

    except Exception as e:
        logger.error(f"Failed to check invoice payment: {e}")


async def create_api_key_from_invoice(
    invoice: LightningInvoice, session: AsyncSession
) -> ApiKey:
    mint_url = invoice.mint_url or settings.primary_mint
    wallet = await get_wallet(mint_url, "sat")
    await _mint_operation(
        lambda: wallet.mint(invoice.amount_sats, quote_id=invoice.payment_hash),
        op_name="invoice_mint_create",
        mint_url=mint_url,
    )

    dummy_token = f"invoice-{invoice.id}-{invoice.payment_hash}"
    hashed_key = hashlib.sha256(dummy_token.encode()).hexdigest()

    api_key = ApiKey(
        hashed_key=hashed_key,
        balance=invoice.amount_sats * 1000,  # Convert to msats
        refund_currency="sat",
        refund_mint_url=mint_url,
        balance_limit=invoice.balance_limit,
        balance_limit_reset=invoice.balance_limit_reset,
        validity_date=invoice.validity_date,
    )

    session.add(api_key)
    await session.flush()

    return api_key


async def topup_api_key_from_invoice(
    invoice: LightningInvoice, session: AsyncSession
) -> None:
    mint_url = invoice.mint_url or settings.primary_mint
    wallet = await get_wallet(mint_url, "sat")
    await _mint_operation(
        lambda: wallet.mint(invoice.amount_sats, quote_id=invoice.payment_hash),
        op_name="invoice_mint_topup",
        mint_url=mint_url,
    )

    if not invoice.api_key_hash:
        raise ValueError("No API key associated with topup invoice")

    api_key = await session.get(ApiKey, invoice.api_key_hash)
    if not api_key:
        raise ValueError("Associated API key not found")

    api_key.balance += invoice.amount_sats * 1000  # Convert to msats
    await session.flush()


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
