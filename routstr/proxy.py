import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from sqlmodel import select

from .algorithm import create_model_mappings
from .auth import pay_for_request, revert_pay_for_request, validate_bearer_key
from .core import get_logger
from .core.db import (
    ApiKey,
    AsyncSession,
    ModelRow,
    UpstreamProviderRow,
    create_session,
    get_session,
)
from .payment.helpers import (
    calculate_discounted_max_cost,
    check_token_balance,
    create_error_response,
    get_max_cost_for_model,
)
from .payment.models import Model
from .upstream import BaseUpstreamProvider, init_upstreams

logger = get_logger(__name__)
proxy_router = APIRouter()

_upstreams: list[BaseUpstreamProvider] = []
_model_instances: dict[str, Model] = {}  # All aliases -> Model
_provider_map: dict[str, BaseUpstreamProvider] = {}  # All aliases -> Provider
_unique_models: dict[str, Model] = {}  # Unique model.id -> Model (no duplicates)


async def initialize_upstreams() -> None:
    """Initialize upstream providers from database during application startup."""
    global _upstreams
    _upstreams = await init_upstreams()
    logger.info(f"Initialized {len(_upstreams)} upstream providers")
    await refresh_model_maps()


async def reinitialize_upstreams() -> None:
    """Re-initialize upstream providers from database (called after admin changes)."""
    global _upstreams
    _upstreams = await init_upstreams()
    logger.info(
        "Re-initialized upstream providers from admin action",
        extra={"provider_count": len(_upstreams)},
    )
    await refresh_model_maps()


def get_upstreams() -> list[BaseUpstreamProvider]:
    """Get the initialized upstream providers.

    Returns:
        List of upstream provider instances
    """
    return _upstreams


def get_model_instance(model_id: str) -> Model | None:
    """Get Model instance by ID from global cache."""
    return _model_instances.get(model_id)


def get_provider_for_model(model_id: str) -> BaseUpstreamProvider | None:
    """Get UpstreamProvider for model ID from global cache."""
    return _provider_map.get(model_id)


def get_unique_models() -> list[Model]:
    """Get list of unique models (no duplicates from aliases)."""
    return list(_unique_models.values())


async def refresh_model_maps() -> None:
    """Refresh global model and provider maps using the cost-based algorithm."""
    global _model_instances, _provider_map, _unique_models

    # Gather database overrides and disabled models
    async with create_session() as session:
        result = await session.exec(select(ModelRow).where(ModelRow.enabled))
        override_rows = result.all()

        provider_result = await session.exec(select(UpstreamProviderRow))
        providers_by_id = {p.id: p for p in provider_result.all()}

        overrides_by_id: dict[str, tuple[ModelRow, float]] = {
            row.id: (
                row,
                providers_by_id[row.upstream_provider_id].provider_fee
                if row.upstream_provider_id in providers_by_id
                else 1.01,
            )
            for row in override_rows
            if row.upstream_provider_id is not None
        }

        disabled_result = await session.exec(
            select(ModelRow.id).where(ModelRow.enabled == False)  # noqa: E712
        )
        disabled_model_ids = {row for row in disabled_result.all()}

    _model_instances, _provider_map, _unique_models = create_model_mappings(
        upstreams=_upstreams,
        overrides_by_id=overrides_by_id,
        disabled_model_ids=disabled_model_ids,
    )


async def refresh_model_maps_periodically() -> None:
    """Background task to refresh model maps every minute."""
    import asyncio

    while True:
        try:
            await asyncio.sleep(60)
            await refresh_model_maps()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(
                "Error refreshing model maps",
                extra={"error": str(e), "error_type": type(e).__name__},
            )


@proxy_router.api_route("/{path:path}", methods=["GET", "POST"], response_model=None)
async def proxy(
    request: Request, path: str, session: AsyncSession = Depends(get_session)
) -> Response | StreamingResponse:
    headers = dict(request.headers)

    if "x-cashu" not in headers and "authorization" not in headers.keys():
        return create_error_response(
            "unauthorized", "Unauthorized", 401, request=request
        )

    logger.info(  # TODO: move to middleware, async
        "Received proxy request",
        extra={
            "method": request.method,
            "path": path,
            "client_host": request.client.host if request.client else "unknown",
            "user_agent": request.headers.get("user-agent", "unknown")[:100],
        },
    )

    request_body = await request.body()
    request_body_dict = parse_request_body_json(request_body, path)

    model_id = request_body_dict.get("model", "unknown")

    model_obj = get_model_instance(model_id)
    if not model_obj:
        return create_error_response(
            "invalid_model", f"Model '{model_id}' not found", 400, request=request
        )

    upstream = get_provider_for_model(model_id)
    if not upstream:
        return create_error_response(
            "invalid_model",
            f"No provider found for model '{model_id}'",
            400,
            request=request,
        )

    _max_cost_for_model = await get_max_cost_for_model(
        model=model_id, session=session, model_obj=model_obj
    )
    max_cost_for_model = await calculate_discounted_max_cost(
        _max_cost_for_model, request_body_dict, model_obj=model_obj
    )
    check_token_balance(headers, request_body_dict, max_cost_for_model)

    if x_cashu := headers.get("x-cashu", None):
        return await upstream.handle_x_cashu(
            request, x_cashu, path, max_cost_for_model, model_obj
        )

    elif auth := headers.get("authorization", None):
        key = await get_bearer_token_key(headers, path, session, auth)

    else:
        if request.method not in ["GET"]:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {"type": "invalid_request_error", "code": "unauthorized"}
                },
            )

        logger.debug("Processing unauthenticated GET request", extra={"path": path})
        # TODO: why is this needed? can we remove it?
        headers = upstream.prepare_headers(dict(request.headers))
        return await upstream.forward_get_request(request, path, headers)

    # Only pay for request if we have request body data (for completions endpoints)
    if request_body_dict:
        await pay_for_request(key, max_cost_for_model, session)

    # Prepare headers for upstream
    headers = upstream.prepare_headers(dict(request.headers))

    # Forward to upstream and handle response
    response = await upstream.forward_request(
        request,
        path,
        headers,
        request_body,
        key,
        max_cost_for_model,
        session,
        model_obj,
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
            },
        )
        # Return the mapped error response generated earlier rather than masking with 502
        return response

    return response


async def get_bearer_token_key(
    headers: dict, path: str, session: AsyncSession, auth: str
) -> ApiKey:
    """Handle bearer token authentication proxy requests."""
    bearer_key = auth.replace("Bearer ", "") if auth.startswith("Bearer ") else ""
    refund_address = headers.get("Refund-LNURL", None)
    key_expiry_time = headers.get("Key-Expiry-Time", None)

    logger.debug(
        "Processing bearer token",
        extra={
            "path": path,
            "has_refund_address": bool(refund_address),
            "has_expiry_time": bool(key_expiry_time),
            "bearer_key_preview": bearer_key[:20] + "..."
            if len(bearer_key) > 20
            else bearer_key,
        },
    )

    # Validate key_expiry_time header
    if key_expiry_time:
        try:
            key_expiry_time = int(key_expiry_time)  # type: ignore
            logger.debug(
                "Key expiry time validated",
                extra={"expiry_time": key_expiry_time, "path": path},
            )
        except ValueError:
            logger.error(
                "Invalid Key-Expiry-Time header",
                extra={"key_expiry_time": key_expiry_time, "path": path},
            )
            raise HTTPException(
                status_code=400,
                detail="Invalid Key-Expiry-Time: must be a valid Unix timestamp",
            )
        if not refund_address:
            logger.error(
                "Missing Refund-LNURL header with Key-Expiry-Time",
                extra={"path": path, "expiry_time": key_expiry_time},
            )
            raise HTTPException(
                status_code=400,
                detail="Error: Refund-LNURL header required when using Key-Expiry-Time",
            )
    else:
        key_expiry_time = None

    try:
        key = await validate_bearer_key(
            bearer_key,
            session,
            refund_address,
            key_expiry_time,  # type: ignore
        )
        logger.info(
            "Bearer token validated successfully",
            extra={
                "path": path,
                "key_hash": key.hashed_key[:8] + "...",
                "key_balance": key.balance,
            },
        )
        return key
    except Exception as e:
        logger.error(
            "Bearer token validation failed",
            extra={
                "error": str(e),
                "error_type": type(e).__name__,
                "path": path,
                "bearer_key_preview": bearer_key[:20] + "..."
                if len(bearer_key) > 20
                else bearer_key,
            },
        )
        raise


def parse_request_body_json(request_body: bytes, path: str) -> dict[str, Any]:
    request_body_dict = {}
    if request_body:
        try:
            request_body_dict = json.loads(request_body)

            if "max_tokens" in request_body_dict:
                max_tokens_value = request_body_dict["max_tokens"]

                if isinstance(max_tokens_value, int):
                    pass
                else:
                    raise HTTPException(
                        status_code=400,
                        detail={"error": "max_tokens must be an integer"},
                    )

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
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {"type": "invalid_request_error", "code": "invalid_json"}
                },
            )

    return request_body_dict
