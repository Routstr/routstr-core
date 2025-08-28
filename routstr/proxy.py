import json
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from .auth import (
    get_bearer_token_key,
    pay_for_request,
    revert_pay_for_request,
)
from .core import get_logger
from .core.db import AsyncSession, get_session
from .payment.helpers import (
    check_token_balance,
    create_error_response,
    get_max_cost_for_model,
)
from .payment.x_cashu import XCashuUpstreamProvider
from .request import RoutstrRequest
from .upstream import UpstreamProvider

logger = get_logger(__name__)
proxy_router = APIRouter()


def find_upstream_provider(
    model_id: str, cost_filters: dict | None = None
) -> UpstreamProvider:
    providers = [
        XCashuUpstreamProvider(
            url=os.getenv("UPSTREAM_BASE_URL") or "",
            api_key=os.getenv("UPSTREAM_API_KEY") or "",
        ),
    ]
    return providers[0]


@proxy_router.api_route("/{path:path}", methods=["GET", "POST"], response_model=None)
async def proxy_to_upstream(
    request: Request, path: str, db_session: AsyncSession = Depends(get_session)
) -> Response | StreamingResponse:
    request_body = json.loads(await request.body())
    headers = dict(request.headers)
    routstr_request = RoutstrRequest.from_request(request_body, headers, path)

    # TODO: should raise proper HTTPExceptions instead of ValueError
    # TODO: add better validation and error handling
    routstr_request.validate()

    required_reserved_balance_msats = get_max_cost_for_model(
        model=routstr_request.model_id
    )

    # TODO fix multiple db_session commits

    # without claiming the token if new token provided, else just check db balance
    await raise_not_enough_balance(
        routstr_request=routstr_request,
        required_balance_msats=required_reserved_balance_msats,
        db_session=db_session,
    )

    # if new cashu token is provided, it is redeemed here
    # temp_balance = await routstr_request.get_or_create_balance(db_session)

    upstream_provider = find_upstream_provider(routstr_request.model_id)

    await routstr_request.reserve_balance(db_session, required_reserved_balance_msats)

    response = await upstream_provider.forward(routstr_request)

    if response.status_code != 200:
        await revert_pay_for_request(
            await routstr_request.get_or_create_balance(db_session),
            db_session,
            required_reserved_balance_msats,
        )
        raise ValueError("Upstream request failed")  # TODO change to HTTPException

    return response


async def raise_not_enough_balance(
    routstr_request: RoutstrRequest,
    required_balance_msats: int,
    db_session: AsyncSession,
) -> None:
    if routstr_request.cashu_token:
        msat_multiplier = 1000 if routstr_request.cashu_token.unit == "sat" else 1
        token_amount_msats = routstr_request.cashu_token.amount * msat_multiplier
        if required_balance_msats > token_amount_msats:
            raise HTTPException(
                status_code=413,
                detail={
                    "reason": "Insufficient balance",
                    "amount_required_msat": required_balance_msats,
                    "model_id": routstr_request.model_id,
                    "type": "minimum_balance_required",
                },
            )
    elif routstr_request.balance_id:
        balance = await routstr_request.get_or_create_balance(db_session)
        if required_balance_msats > balance.balance:
            raise HTTPException(
                status_code=413,
                detail={
                    "reason": "Insufficient balance",
                    "amount_required_msat": required_balance_msats,
                    "model_id": routstr_request.model_id,
                    "type": "minimum_balance_required",
                },
            )
    else:
        raise HTTPException(
            status_code=400,
            detail={"reason": "No balance found", "type": "no_balance_found"},
        )


@proxy_router.api_route("/{path:path}", methods=["GET", "POST"], response_model=None)
async def proxy(
    request: Request, path: str, session: AsyncSession = Depends(get_session)
) -> Response | StreamingResponse:
    """Main proxy endpoint handler."""
    request_body = await request.body()
    headers = dict(request.headers)
    upstream_provider = XCashuUpstreamProvider(
        url=os.getenv("UPSTREAM_BASE_URL") or "",
        api_key=os.getenv("UPSTREAM_API_KEY") or "",
    )

    if "x-cashu" not in headers and "authorization" not in headers.keys():
        return create_error_response(
            "unauthorized", "Unauthorized", 401, request=request
        )

    logger.info(
        "Received proxy request",
        extra={
            "method": request.method,
            "path": path,
            "client_host": request.client.host if request.client else "unknown",
            "user_agent": request.headers.get("user-agent", "unknown")[:100],
        },
    )

    # Parse JSON body if present, handle empty/invalid JSON
    request_body_dict = {}
    if request_body:
        try:
            request_body_dict = json.loads(request_body)
            logger.debug(
                "Request body parsed",
                extra={
                    "path": path,
                    "body_keys": list(request_body_dict.keys()),
                    "model": request_body_dict.get("model", "not_specified"),
                },
            )
        except json.JSONDecodeError as e:
            logger.error(
                "Invalid JSON in request body",
                extra={
                    "error": str(e),
                    "path": path,
                    "body_preview": request_body[:200].decode(errors="ignore")
                    if request_body
                    else "empty",
                },
            )
            return Response(
                content=json.dumps(
                    {"error": {"type": "invalid_request_error", "code": "invalid_json"}}
                ),
                status_code=400,
                media_type="application/json",
            )

    model = request_body_dict.get("model", "unknown")
    max_cost_for_model = get_max_cost_for_model(model=model)
    check_token_balance(headers, request_body_dict, max_cost_for_model)

    # Handle authentication
    if x_cashu := headers.get("x-cashu", None):
        logger.info(
            "Processing X-Cashu payment",
            extra={
                "path": path,
                "token_preview": x_cashu[:20] + "..." if len(x_cashu) > 20 else x_cashu,
            },
        )
        return await upstream_provider.x_cashu_handler(
            request, x_cashu, path, max_cost_for_model
        )

    elif auth := headers.get("authorization", None):
        logger.debug(
            "Processing bearer token authentication",
            extra={
                "path": path,
                "token_preview": auth[:20] + "..." if len(auth) > 20 else auth,
            },
        )
        key = await get_bearer_token_key(headers, path, session, auth)

    # TODO: why is this needed? can we remove it?
    # else:
    #     if request.method not in ["GET"]:
    #         logger.warning(
    #             "Unauthorized request - no authentication provided",
    #             extra={"method": request.method, "path": path},
    #         )
    #         return Response(
    #             content=json.dumps({"detail": "Unauthorized"}),
    #             status_code=401,
    #             media_type="application/json",
    #         )

    #     logger.debug("Processing unauthenticated GET request", extra={"path": path})
    #     headers = upstream_provider.prepare_upstream_headers(dict(request.headers))
    #     return await upstream_provider.forward_to_upstream(
    #         request,
    #         path,
    #         headers,
    #         request_body,
    #         key,
    #         max_cost_for_model,
    #         session,
    #     )

    # Only pay for request if we have request body data (for completions endpoints)
    if request_body_dict:
        logger.info(
            "Processing payment for request",
            extra={
                "path": path,
                "key_hash": key.hashed_key[:8] + "...",
                "key_balance_before": key.balance,
                "model": request_body_dict.get("model", "unknown"),
            },
        )

        try:
            await pay_for_request(key, max_cost_for_model, session)
            logger.info(
                "Payment processed successfully",
                extra={
                    "path": path,
                    "key_hash": key.hashed_key[:8] + "...",
                    "key_balance_after": key.balance,
                    "model": request_body_dict.get("model", "unknown"),
                },
            )
        except Exception as e:
            logger.error(
                "Payment processing failed",
                extra={
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "path": path,
                    "key_hash": key.hashed_key[:8] + "...",
                },
            )
            raise

    # Prepare headers for upstream
    headers = upstream_provider.prepare_upstream_headers(dict(request.headers))

    # Forward to upstream and handle response
    response = await upstream_provider.forward_to_upstream(
        request, path, headers, request_body, key, max_cost_for_model, session
    )

    if response.status_code != 200:
        await revert_pay_for_request(key, session, max_cost_for_model)
        logger.warning(
            "Upstream request failed, revert payment",
            extra={
                "status_code": response.status_code,
                "path": path,
                "key_hash": key.hashed_key[:8] + "...",
                "key_balance": key.balance,
                "max_cost_for_model": max_cost_for_model,
                "upstream_headers": response.headers
                if hasattr(response, "headers")
                else None,
                "upstream_response": response.body
                if hasattr(response, "body")
                else None,
            },
        )
        request_id = (
            request.state.request_id if hasattr(request.state, "request_id") else None
        )
        raise HTTPException(
            status_code=502,
            detail=f"Upstream request failed, please contact support with request id: {request_id}",
        )

    return response
