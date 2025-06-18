from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, Header, HTTPException

from .auth import validate_bearer_key
from .cashu import (
    WALLET,
    credit_balance,
    delete_key_if_zero_balance,
    refund_balance,
)
from .db import ApiKey, AsyncSession, get_session

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
) -> dict[str, int]:
    amount_msats = await credit_balance(cashu_token, key, session)
    return {"msats": amount_msats}


@wallet_router.post("/refund")
async def refund_wallet_endpoint(
    key: ApiKey = Depends(get_key_from_header),
    session: AsyncSession = Depends(get_session),
) -> dict:
    remaining_balance_msats = key.balance

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
            raise HTTPException(
                status_code=400, detail="Balance too small to refund (less than 1 sat)"
            )

        token = await WALLET.send(remaining_balance_sats)
        result = {"msats": remaining_balance_msats, "recipient": None, "token": token}

    # Only after successful refund, zero out the balance
    key.balance = 0
    session.add(key)
    await session.commit()
    await delete_key_if_zero_balance(key, session)

    return result


@wallet_router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE"],
    include_in_schema=False,
    response_model=None,
)
async def wallet_catch_all(path: str) -> NoReturn:
    raise HTTPException(
        status_code=404, detail="Not found check /docs for available endpoints"
    )
