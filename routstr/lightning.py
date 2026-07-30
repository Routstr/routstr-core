import asyncio
import hashlib
import secrets
import time

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm.attributes import set_committed_value
from sqlmodel import col, select, update
from sqlmodel.ext.asyncio.session import AsyncSession

from .core.db import ApiKey, LightningInvoice, create_session, get_session
from .core.logging import get_logger
from .core.settings import settings
from .wallet import get_wallet, wallet_operation_guard

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
            api_key_hash=api_key_token[3:] if api_key_token else None,
            purpose=request.purpose,
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
    # Minting makes proofs visible before database finalization. Share the
    # cross-process wallet guard with owner payout so that visibility and the
    # corresponding liability commit are observed atomically by the payout loop.
    async with wallet_operation_guard():
        await _check_invoice_payment_locked(invoice, session)


async def _check_invoice_payment_locked(
    invoice: LightningInvoice, session: AsyncSession
) -> None:
    minted = False
    invoice_id = invoice.id
    invoice_purpose = invoice.purpose
    invoice_amount_sats = invoice.amount_sats
    invoice_payment_hash = invoice.payment_hash
    finalized_api_key_hash = invoice.api_key_hash
    try:
        # A preceding invoice lookup starts a transaction. End it before the
        # potentially slow mint request so it cannot pin a pool connection.
        await session.commit()

        wallet = await get_wallet(settings.primary_mint, "sat")
        mint_status = await wallet.get_mint_quote(invoice_payment_hash)
        if not mint_status.paid:
            return

        # Do not redeem a paid top-up quote if its target has already been
        # pruned. This validation owns a short-lived session and releases its
        # connection before mint redemption starts.
        if invoice_purpose == "topup":
            if not finalized_api_key_hash:
                raise ValueError("No API key associated with topup invoice")
            async with create_session() as validation_session:
                target = await validation_session.get(ApiKey, finalized_api_key_hash)
                if target is None:
                    terminal = await validation_session.exec(  # type: ignore[call-overload]
                        update(LightningInvoice)
                        .where(
                            col(LightningInvoice.id) == invoice_id,
                            col(LightningInvoice.status) == "pending",
                        )
                        .values(status="reconciliation_required")
                    )
                    await validation_session.commit()
                    if terminal.rowcount == 1:
                        set_committed_value(
                            invoice, "status", "reconciliation_required"
                        )
                    else:
                        committed_invoice = await validation_session.get(
                            LightningInvoice, invoice_id
                        )
                        if committed_invoice is not None:
                            set_committed_value(
                                invoice, "status", committed_invoice.status
                            )
                    logger.critical(
                        "Paid topup invoice target API key was not found; reconciliation required",
                        extra={"invoice_id": invoice_id},
                    )
                    return

        # The mint enforces single-use quotes, so a concurrent checker that
        # races us here fails inside wallet.mint rather than double-minting.
        await wallet.mint(invoice_amount_sats, quote_id=invoice_payment_hash)
        minted = True

        # Paid finalization owns a fresh session. The API/watcher session is
        # never rolled back by this function, so its invoice and sibling ORM
        # objects remain usable after a DB failure or lost CAS race.
        async with create_session() as finalization_session:
            if invoice_purpose == "create":
                api_key = await _create_api_key_record(invoice, finalization_session)
                finalized_api_key_hash = api_key.hashed_key
            elif invoice_purpose == "topup":
                await _credit_topup_record(invoice, finalization_session)

            # Conditional transition guards against double-credit: the credit
            # above and this status flip commit atomically, and a lost race
            # rolls both back in the owned finalization session.
            paid_at = int(time.time())
            finalized = await finalization_session.exec(  # type: ignore[call-overload]
                update(LightningInvoice)
                .where(
                    col(LightningInvoice.id) == invoice_id,
                    col(LightningInvoice.status) == "pending",
                )
                .values(
                    status="paid",
                    paid_at=paid_at,
                    api_key_hash=finalized_api_key_hash,
                )
            )
            if finalized.rowcount != 1:
                await finalization_session.rollback()
                committed_invoice = await finalization_session.get(
                    LightningInvoice, invoice_id
                )
                await finalization_session.commit()
                if committed_invoice is not None:
                    # A concurrent finalizer won the CAS. Publish only the
                    # state observed from the database after ending the owned
                    # read transaction; never refresh the caller's session.
                    set_committed_value(
                        invoice, "api_key_hash", committed_invoice.api_key_hash
                    )
                    set_committed_value(invoice, "status", committed_invoice.status)
                    set_committed_value(invoice, "paid_at", committed_invoice.paid_at)
                return
            await finalization_session.commit()

        # Only publish finalized values to the caller-owned object after the
        # owned transaction has committed successfully.
        set_committed_value(invoice, "api_key_hash", finalized_api_key_hash)
        set_committed_value(invoice, "status", "paid")
        set_committed_value(invoice, "paid_at", paid_at)

        logger.info(
            "Lightning invoice paid",
            extra={
                "invoice_id": invoice_id,
                "amount_sats": invoice_amount_sats,
                "purpose": invoice_purpose,
                "api_key_hash": finalized_api_key_hash[:8] + "..."
                if finalized_api_key_hash
                else None,
            },
        )
    except BaseException as e:
        # BaseException so task cancellation (e.g. client disconnect) after a
        # successful mint still triggers the reconciliation alert. Any rollback
        # belongs to create_session(), never to the caller-owned session.
        if minted:
            logger.critical(
                "Invoice mint succeeded but DB finalization failed; reconciliation required",
                extra={"invoice_id": invoice_id, "purpose": invoice_purpose},
            )
        if not isinstance(e, Exception):
            raise
        logger.error(f"Failed to check invoice payment: {e}")


async def _create_api_key_record(
    invoice: LightningInvoice, session: AsyncSession
) -> ApiKey:
    dummy_token = f"invoice-{invoice.id}-{invoice.payment_hash}"
    hashed_key = hashlib.sha256(dummy_token.encode()).hexdigest()
    api_key = ApiKey(
        hashed_key=hashed_key,
        balance=invoice.amount_sats * 1000,
        refund_currency="sat",
        refund_mint_url=settings.primary_mint,
        balance_limit=invoice.balance_limit,
        balance_limit_reset=invoice.balance_limit_reset,
        validity_date=invoice.validity_date,
    )
    session.add(api_key)
    await session.flush()
    return api_key


async def _credit_topup_record(
    invoice: LightningInvoice, session: AsyncSession
) -> None:
    if not invoice.api_key_hash:
        raise ValueError("No API key associated with topup invoice")
    credited = await session.exec(  # type: ignore[call-overload]
        update(ApiKey)
        .where(col(ApiKey.hashed_key) == invoice.api_key_hash)
        .values(balance=col(ApiKey.balance) + invoice.amount_sats * 1000)
    )
    if credited.rowcount != 1:
        raise ValueError("Associated API key not found")


INVOICE_WATCH_INTERVAL_SECONDS = 5
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
