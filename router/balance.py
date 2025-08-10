from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, Header, HTTPException

from .auth import validate_bearer_key
from .core.db import ApiKey, AsyncSession, get_session
from .wallet import CurrencyUnit, credit_balance, send_to_lnurl, send_token

router = APIRouter()
balance_router = APIRouter(prefix="/v1/balance")


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
@router.get("/", include_in_schema=False)
async def account_info(key: ApiKey = Depends(get_key_from_header)) -> dict:
    return {
        "api_key": "sk-" + key.hashed_key,
        "balance": key.balance,
    }


@router.get("/info")
async def wallet_info(key: ApiKey = Depends(get_key_from_header)) -> dict:
    return {
        "api_key": "sk-" + key.hashed_key,
        "balance": key.balance,
    }


@router.post("/topup")
async def topup_wallet_endpoint(
    cashu_token: str,
    key: ApiKey = Depends(get_key_from_header),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    cashu_token = cashu_token.replace("\n", "").replace("\r", "").replace("\t", "")
    if len(cashu_token) < 10 or "cashu" not in cashu_token:
        raise HTTPException(status_code=400, detail="Invalid token format")
    try:
        amount_msats = await credit_balance(cashu_token, key, session)
    except ValueError as e:
        error_msg = str(e)
        if "already spent" in error_msg.lower():
            raise HTTPException(status_code=400, detail="Token already spent")
        elif "invalid" in error_msg.lower() or "decode" in error_msg.lower():
            raise HTTPException(status_code=400, detail="Invalid token format")
        else:
            raise HTTPException(status_code=400, detail="Failed to redeem token")
    except Exception:
        raise HTTPException(status_code=500, detail="Internal server error")
    return {"msats": amount_msats}


@router.post("/refund")
async def refund_wallet_endpoint(
    key: ApiKey = Depends(get_key_from_header),
    session: AsyncSession = Depends(get_session),
) -> dict:
    remaining_balance_msats = key.balance

    if remaining_balance_msats == 0:
        raise HTTPException(status_code=400, detail="No balance to refund")

    # Perform refund operation first, before modifying balance
    try:
        if key.refund_address:
            await send_to_lnurl(remaining_balance_msats, CurrencyUnit.msat, key.refund_address)
            result = {"recipient": key.refund_address, "msats": remaining_balance_msats}
        else:
            # Convert msats to sats for cashu wallet
            remaining_balance_sats = remaining_balance_msats // 1000
            if remaining_balance_sats == 0:
                raise HTTPException(
                    status_code=400, detail="Balance too small to refund (less than 1 sat)"
                )

            # TODO: choose currency and mint based on what user has configured
            token = await send_token(remaining_balance_sats, "sat")

            result = {"msats": remaining_balance_msats, "recipient": None, "token": token}
    except HTTPException:
        # Re-raise HTTP exceptions (like 400 for balance too small)
        raise
    except Exception as e:
        # If refund fails, don't modify the database
        error_msg = str(e)
        if ("mint" in error_msg.lower() or "connection" in error_msg.lower() or 
            isinstance(e, Exception) and "ConnectError" in str(type(e))):
            raise HTTPException(status_code=503, detail="Mint service unavailable")
        else:
            raise HTTPException(status_code=500, detail="Refund failed")

    await session.delete(key)
    await session.commit()

    return result


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE"],
    include_in_schema=False,
    response_model=None,
)
async def wallet_catch_all(path: str) -> NoReturn:
    raise HTTPException(
        status_code=404, detail="Not found check /docs for available endpoints"
    )


balance_router.include_router(router)
deprecated_wallet_router = APIRouter(prefix="/v1/wallet", include_in_schema=False)
deprecated_wallet_router.include_router(router)
