"""Top up an API key with XNO sent straight to the operator's own address.

Off unless RECEIVE_NANO_ADDRESS, NANO_RPC_URL and NANO_RAW_PER_SAT are all set;
until then both routes answer exactly like an unknown path.
"""

import secrets
import time
from typing import TypeVar

from fastapi import APIRouter, Depends, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select, update
from sqlmodel.ext.asyncio.session import AsyncSession

from .core.db import ApiKey, NanoTopup, get_session
from .core.logging import get_logger
from .core.not_found import build_not_found_response
from .core.settings import settings
from .lightning import (
    _credit_topup_record,
    _extract_bearer_api_key,
    _invoice_error,
    _InvoiceSettlement,
    generate_invoice_id,
)
from .nano import (
    RAW_TAG_MODULUS,
    NanoRpcClient,
    NanoRpcError,
    configured_raw_per_sat,
    get_nano_rpc,
    is_block_hash,
    is_valid_account,
    same_account,
    tagged_quote_raw,
)

logger = get_logger(__name__)

nano_router = APIRouter(prefix="/v2/nano", include_in_schema=False)

NANO_INVOICE_TTL_SECONDS = 3600
_TAG_ATTEMPTS = 8

_Body = TypeVar("_Body", bound=BaseModel)


class NanoInvoiceRequest(BaseModel):
    amount_sats: int = Field(gt=0, le=1_000_000, description="Amount in satoshis")


class NanoInvoiceResponse(BaseModel):
    invoice_id: str
    address: str
    amount_raw: str
    amount_sats: int
    expires_at: int
    uri: str


class NanoClaimRequest(BaseModel):
    invoice_id: str
    block_hash: str


class NanoClaimResponse(BaseModel):
    invoice_id: str
    status: str
    amount_sats: int
    block_hash: str
    paid_at: int


def _nano_offer() -> tuple[str, int] | None:
    address = settings.receive_nano_address.strip()
    rate = configured_raw_per_sat()
    if not address or rate is None or not settings.nano_rpc_url.strip():
        return None
    if not is_valid_account(address):
        logger.warning("RECEIVE_NANO_ADDRESS is not a valid nano_ account")
        return None
    return address, rate


def _not_offered(request: Request) -> Response:
    return build_not_found_response(request, request.url.path.lstrip("/"))


def _error(status_code: int, message: str, code: str) -> Exception:
    return _invoice_error(
        status_code, message, "invalid_request_error", code, structured=True
    )


async def _read_body(request: Request, model: type[_Body]) -> _Body:
    # Parsed here rather than by FastAPI so a node without the offer answers
    # 404 for any body, exactly like an unknown path.
    try:
        return model.model_validate_json(await request.body())
    except ValidationError as error:
        raise RequestValidationError(error.errors())


async def _unused_tag(session: AsyncSession, now: int) -> int:
    for _ in range(_TAG_ATTEMPTS):
        tag = secrets.randbelow(RAW_TAG_MODULUS - 1) + 1
        taken = await session.exec(
            select(NanoTopup.id).where(
                col(NanoTopup.raw_tag) == tag,
                col(NanoTopup.status) == "pending",
                col(NanoTopup.expires_at) >= now,
            )
        )
        if taken.first() is None:
            return tag
    raise _invoice_error(
        503,
        "Too many open Nano invoices; retry shortly",
        "api_error",
        "nano_invoice_unavailable",
        structured=True,
    )


@nano_router.post("/invoice", response_model=None)
async def create_nano_invoice(
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> NanoInvoiceResponse | Response:
    offer = _nano_offer()
    if offer is None:
        return _not_offered(request)
    address, raw_per_sat = offer
    body = await _read_body(request, NanoInvoiceRequest)

    api_key_token = _extract_bearer_api_key(authorization)
    if not api_key_token:
        raise _error(
            401,
            "Authorization bearer api key is required for topup",
            "topup_authorization_required",
        )
    if not api_key_token.startswith("sk-"):
        raise _error(
            400,
            "Invalid API key format. Expected an 'sk-...' API key.",
            "topup_invalid_api_key_format",
        )
    api_key = await session.get(ApiKey, api_key_token[3:])
    if not api_key:
        raise _error(404, "API key not found", "topup_api_key_not_found")

    now = int(time.time())
    tag = await _unused_tag(session, now)
    amount_raw = tagged_quote_raw(body.amount_sats * 1000, raw_per_sat, tag)
    topup = NanoTopup(
        id=generate_invoice_id(),
        api_key_hash=api_key.hashed_key,
        amount_sats=body.amount_sats,
        amount_raw=str(amount_raw),
        raw_tag=tag,
        address=address,
        created_at=now,
        expires_at=now + NANO_INVOICE_TTL_SECONDS,
    )
    session.add(topup)
    await session.commit()

    logger.info(
        "Nano invoice created",
        extra={
            "invoice_id": topup.id,
            "amount_sats": body.amount_sats,
            "expires_at": topup.expires_at,
        },
    )
    return NanoInvoiceResponse(
        invoice_id=topup.id,
        address=address,
        amount_raw=topup.amount_raw,
        amount_sats=topup.amount_sats,
        expires_at=topup.expires_at,
        uri=f"nano:{address}?amount={topup.amount_raw}",
    )


@nano_router.post("/claim", response_model=None)
async def claim_nano_invoice(
    request: Request,
    session: AsyncSession = Depends(get_session),
    rpc: NanoRpcClient = Depends(get_nano_rpc),
) -> NanoClaimResponse | Response:
    if _nano_offer() is None:
        return _not_offered(request)
    body = await _read_body(request, NanoClaimRequest)
    if not is_block_hash(body.block_hash):
        raise _error(400, "block_hash must be 64 hex characters", "invalid_block_hash")
    block_hash = body.block_hash.upper()

    topup = await session.get(NanoTopup, body.invoice_id)
    if topup is None:
        raise _error(404, "Invoice not found", "invoice_not_found")
    if topup.status != "pending":
        raise _error(409, "Invoice already paid", "invoice_already_paid")
    used = await session.exec(
        select(NanoTopup.id).where(col(NanoTopup.block_hash) == block_hash)
    )
    if used.first() is not None:
        raise _error(409, "Block already claimed", "nano_block_already_claimed")

    try:
        block = await rpc.block_info(block_hash)
    except NanoRpcError as error:
        logger.warning(
            "Nano block lookup failed",
            extra={"invoice_id": topup.id, "error": str(error)},
        )
        raise _invoice_error(
            502,
            "Nano node could not confirm the block; retry later",
            "api_error",
            "nano_rpc_error",
            structured=True,
        )

    if not block.confirmed:
        raise _error(409, "Block is not confirmed yet", "nano_block_unconfirmed")
    if block.subtype != "send":
        raise _error(400, "Block is not a send", "nano_block_not_send")
    if not same_account(block.destination, topup.address):
        raise _error(
            400, "Block does not pay this node's address", "nano_wrong_destination"
        )
    quoted = int(topup.amount_raw)
    if block.amount_raw < quoted or block.amount_raw % RAW_TAG_MODULUS != topup.raw_tag:
        raise _error(
            400, "Block amount does not match the invoice", "nano_amount_mismatch"
        )
    if not topup.created_at <= block.local_timestamp <= topup.expires_at:
        raise _error(
            400,
            "Block was not sent while the invoice was open",
            "nano_invoice_expired",
        )

    paid_at = int(time.time())
    try:
        claim = await session.exec(  # type: ignore[call-overload]
            update(NanoTopup)
            .where(
                col(NanoTopup.id) == topup.id,
                col(NanoTopup.status) == "pending",
            )
            .values(status="paid", block_hash=block_hash, paid_at=paid_at)
            .execution_options(synchronize_session=False)
        )
    except IntegrityError:
        await session.rollback()
        raise _error(409, "Block already claimed", "nano_block_already_claimed")
    if claim.rowcount != 1:
        await session.rollback()
        raise _error(409, "Invoice already paid", "invoice_already_paid")

    try:
        await _credit_topup_record(
            _InvoiceSettlement(
                id=topup.id,
                payment_hash=block_hash,
                amount_sats=topup.amount_sats,
                purpose="topup",
                api_key_hash=topup.api_key_hash,
                mint_url=None,
                validity_date=None,
            ),
            session,
        )
    except ValueError:
        await session.rollback()
        raise _error(404, "API key not found", "topup_api_key_not_found")
    await session.commit()

    logger.info(
        "Nano top-up credited",
        extra={
            "invoice_id": topup.id,
            "amount_sats": topup.amount_sats,
            "block_hash": block_hash,
        },
    )
    return NanoClaimResponse(
        invoice_id=topup.id,
        status="paid",
        amount_sats=topup.amount_sats,
        block_hash=block_hash,
        paid_at=paid_at,
    )
