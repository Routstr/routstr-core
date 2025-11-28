import hashlib
from typing import Annotated

from fastapi import Depends, Header, HTTPException
from sqlalchemy.exc import IntegrityError

from .core import get_logger
from .core.db import AsyncSession, TemporaryCredit, get_session
from .core.settings import settings
from .payment.wallet import credit_balance, deserialize_token_from_string

logger = get_logger(__name__)


async def api_key_to_credit(key: str, db_session: AsyncSession) -> TemporaryCredit:
    key = key[3:] if key.startswith("sk-") else key
    if existing_credit := await db_session.get(TemporaryCredit, key):
        return existing_credit

    raise HTTPException(status_code=401, detail="Invalid API-KEY")


async def cashu_token_to_credit(
    cashu_token: str, db_session: AsyncSession
) -> TemporaryCredit:
    try:
        token_hash = hashlib.sha256(cashu_token.encode()).hexdigest()
        token_obj = deserialize_token_from_string(cashu_token)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid token format")

    if existing_credit := await db_session.get(TemporaryCredit, token_hash):
        return existing_credit

    # create new TemporaryCredit
    if token_obj.mint in settings.cashu_mints:
        refund_currency = token_obj.unit
        refund_mint_url = token_obj.mint
    else:
        refund_mint_url = settings.primary_mint
        refund_currency = "sat"

    new_credit = TemporaryCredit(
        hashed_key=token_hash,
        refund_currency=refund_currency,
        refund_mint_url=refund_mint_url,
    )
    db_session.add(new_credit)

    try:
        await db_session.flush()
    except IntegrityError:  # fallback to api key in case of race condition
        await db_session.rollback()
        return await api_key_to_credit(f"sk-{token_hash}", db_session)

    try:
        msats = await credit_balance(cashu_token, new_credit, db_session)
    except Exception as e:
        await db_session.rollback()
        logger.error(
            "Token redemption failed",
            extra={"error": str(e)},
        )
        raise HTTPException(status_code=400, detail="Token redemption failed")

    await db_session.refresh(new_credit)
    await db_session.commit()

    logger.info(
        "New TemporaryCredit created from CashuToken",
        extra={"key_hash": token_hash[:8] + "...", "amount_msats": msats},
    )

    return new_credit


async def nwc_to_credit(
    connection_string: str, db_session: AsyncSession
) -> TemporaryCredit:
    raise NotImplementedError


async def get_credit(
    authorization: Annotated[str, Header(...)],
    db_session: AsyncSession = Depends(get_session),
) -> TemporaryCredit:
    authorization = (
        authorization[7:] if authorization.startswith("Bearer ") else authorization
    )

    if authorization.startswith("cashu"):
        return await cashu_token_to_credit(authorization, db_session)
    elif authorization.startswith("sk-"):
        return await api_key_to_credit(authorization, db_session)
    elif authorization.startswith("nwc"):
        return await nwc_to_credit(authorization, db_session)
    else:
        raise HTTPException(status_code=401, detail="Unable to parse bearer token")
