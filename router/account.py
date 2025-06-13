from typing import Annotated
import logging
from fastapi import APIRouter, Header, HTTPException, Depends

from .auth import validate_bearer_key
from .cashu import refund_balance, credit_balance, WALLET
from .db import ApiKey, AsyncSession, get_session

logger = logging.getLogger(__name__)

wallet_router = APIRouter(prefix="/v1/wallet")


async def get_key_from_header(
    authorization: Annotated[str, Header(...)],
    session: AsyncSession = Depends(get_session),
) -> ApiKey:
    if authorization.startswith("Bearer "):
        return await validate_bearer_key(authorization[7:], session)

    raise HTTPException(
        status_code=401,
        detail="Invalid authorization. Use 'Bearer <cashu-token>' or 'Bearer <api-key>'",
    )

# TODO: remove this endpoint when frontend is updated
@wallet_router.get("/")
async def account_info(key: ApiKey = Depends(get_key_from_header)) -> dict:
    return {
        "api_key": "sk-" + key.hashed_key,
        "balance": key.balance,
    }

@wallet_router.get("/info")
async def wallet_info(key: ApiKey = Depends(get_key_from_header)) -> dict:
    return {
        "api_key": "sk-" + key.hashed_key,
        "balance": key.balance,
    }


@wallet_router.post("/topup")
async def topup_wallet_endpoint(
    cashu_token: str,
    key: ApiKey = Depends(get_key_from_header),
    session: AsyncSession = Depends(get_session),
):
    logger.info("Topup requested for key %s...", key.hashed_key[:10])
    result = await credit_balance(cashu_token, key, session)
    if isinstance(result, int):
        logger.info("Credited %s msats to key %s...", result, key.hashed_key[:10])
    else:
        logger.info("Topup result for key %s: %s", key.hashed_key[:10], result)
    return result


@wallet_router.post("/refund")
async def refund_wallet_endpoint(
    key: ApiKey = Depends(get_key_from_header),
    session: AsyncSession = Depends(get_session),
) -> dict:
    remaining_balance_msats = key.balance
    logger.info(
        "Refund requested for key %s... balance %s msats",
        key.hashed_key[:10],
        remaining_balance_msats,
    )
    
    if remaining_balance_msats == 0:
        raise HTTPException(status_code=400, detail="No balance to refund")
    
    # Perform refund operation first, before modifying balance
    if key.refund_address:
        await refund_balance(remaining_balance_msats, key, session)
        result = {"recipient": key.refund_address, "msats": remaining_balance_msats}
    else:
        # Convert msats to sats for cashu wallet
        remaining_balance_sats = remaining_balance_msats // 1000
        if remaining_balance_sats == 0:
            raise HTTPException(status_code=400, detail="Balance too small to refund (less than 1 sat)")
        
        token = await WALLET.send(remaining_balance_sats)
        result = {"msats": remaining_balance_msats, "recipient": None, "token": token}
    
    # Only after successful refund, zero out the balance
    key.balance = 0
    session.add(key)
    await session.commit()

    logger.info(
        "Refunded %s msats from key %s...", remaining_balance_msats, key.hashed_key[:10]
    )
    return result


@wallet_router.api_route(
    "/{path:path}", methods=["GET", "POST", "PUT", "DELETE"], include_in_schema=False
)
async def wallet_catch_all(path: str):
    raise HTTPException(status_code=404, detail="Not found check /docs for available endpoints")
