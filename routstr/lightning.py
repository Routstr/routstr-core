import hashlib
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from .core.db import ApiKey, LightningInvoice, get_session
from .core.logging import get_logger
from .core.settings import settings
from .wallet import get_wallet

logger = get_logger(__name__)

lightning_router = APIRouter(prefix="/lightning")


class InvoiceCreateRequest(BaseModel):
    amount_sats: int = Field(gt=0, le=1_000_000, description="Amount in satoshis")
    purpose: str = Field(description="create or topup", pattern="^(create|topup)$")
    api_key: str | None = Field(
        default=None, description="Required for topup operations"
    )


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


async def generate_lightning_invoice(
    amount_sats: int, description: str
) -> tuple[str, str]:
    wallet = await get_wallet(settings.primary_mint, "sat")
    quote = await wallet.request_mint(amount_sats)
    return quote.request, quote.quote


def generate_invoice_id() -> str:
    return secrets.token_urlsafe(16)


@lightning_router.post("/invoice", response_model=InvoiceCreateResponse)
async def create_invoice(
    request: InvoiceCreateRequest,
    session: AsyncSession = Depends(get_session),
) -> InvoiceCreateResponse:
    if request.purpose == "topup" and not request.api_key:
        raise HTTPException(
            status_code=400, detail="api_key is required for topup operations"
        )

    if request.purpose == "topup" and request.api_key:
        if not request.api_key.startswith("sk-"):
            raise HTTPException(status_code=400, detail="Invalid API key format")

        api_key = await session.get(ApiKey, request.api_key[3:])
        if not api_key:
            raise HTTPException(status_code=404, detail="API key not found")

    try:
        description = f"Routstr {request.purpose} {request.amount_sats} sats"
        bolt11, payment_hash = await generate_lightning_invoice(
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
            api_key_hash=request.api_key[3:] if request.api_key else None,
            purpose=request.purpose,
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

    if invoice.status == "pending" and int(time.time()) > invoice.expires_at:
        invoice.status = "expired"
        await session.commit()

    if invoice.status == "pending":
        await check_invoice_payment(invoice, session)

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
        wallet = await get_wallet(settings.primary_mint, "sat")

        mint_status = await wallet.get_mint_quote(invoice.payment_hash)

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
    wallet = await get_wallet(settings.primary_mint, "sat")
    await wallet.mint(invoice.amount_sats, quote_id=invoice.payment_hash)

    dummy_token = f"invoice-{invoice.id}-{invoice.payment_hash}"
    hashed_key = hashlib.sha256(dummy_token.encode()).hexdigest()

    api_key = ApiKey(
        hashed_key=hashed_key,
        balance=invoice.amount_sats * 1000,  # Convert to msats
        refund_currency="sat",
        refund_mint_url=settings.primary_mint,
    )

    session.add(api_key)
    await session.flush()

    return api_key


async def topup_api_key_from_invoice(
    invoice: LightningInvoice, session: AsyncSession
) -> None:
    wallet = await get_wallet(settings.primary_mint, "sat")
    await wallet.mint(invoice.amount_sats, quote_id=invoice.payment_hash)

    if not invoice.api_key_hash:
        raise ValueError("No API key associated with topup invoice")

    api_key = await session.get(ApiKey, invoice.api_key_hash)
    if not api_key:
        raise ValueError("Associated API key not found")

    api_key.balance += invoice.amount_sats * 1000  # Convert to msats
    await session.flush()
