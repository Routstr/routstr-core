import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from sqlmodel import select

from .auth import get_credit
from .core import get_logger
from .core.db import (
    AsyncSession,
    ModelRow,
    UpstreamProviderRow,
    create_session,
    get_session,
)
from .models.algorithm import create_model_mappings
from .models.models import Model
from .payment.cashu import check_token_balance
from .payment.cost import calculate_discounted_max_cost, get_max_cost_for_model
from .payment.helpers import pay_for_request, revert_pay_for_request
from .upstream import BaseUpstreamProvider
from .upstream.helpers import init_upstreams

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


async def parse_request(request: Request) -> tuple[dict, dict, bytes]:
    headers = dict(request.headers)
    body_bytes = await request.body()
    body_dict = parse_request_body_json(body_bytes)
    return headers, body_dict, body_bytes


def ensure_authorization(headers: dict) -> None:
    if "x-cashu" not in headers and "authorization" not in headers.keys():
        # return create_error_response("unauthorized", "Unauthorized", 401, request=request)
        raise HTTPException(status_code=401, detail="Unauthorized")


def log_request(request: Request, path: str) -> None:
    logger.info(
        "Received proxy request",
        extra={
            "method": request.method,
            "path": path,
            "client_host": request.client.host if request.client else "unknown",
            "user_agent": request.headers.get("user-agent", "unknown")[:100],
        },
    )


def get_model_and_upstream(request_body: dict) -> tuple[Model, BaseUpstreamProvider]:
    model_id = request_body.get("model", "unknown")

    if not (model := get_model_instance(model_id)):
        raise HTTPException(status_code=400, detail=f"Model '{model_id}' not found")

    if not (upstream := get_provider_for_model(model_id)):
        raise HTTPException(status_code=400, detail=f"No provider with '{model_id}'")

    return model, upstream


@proxy_router.api_route("/{path:path}", methods=["GET", "POST"], response_model=None)
async def proxy(
    request: Request,
    path: str,
    session: AsyncSession = Depends(get_session),
) -> Response | StreamingResponse:
    headers, body_dict, body_bytes = await parse_request(request)

    ensure_authorization(headers)

    log_request(request, path)

    model, upstream = get_model_and_upstream(body_dict)

    _max_cost_for_model = get_max_cost_for_model(model_obj=model)
    max_cost_for_model = await calculate_discounted_max_cost(
        _max_cost_for_model, body_dict, model_obj=model
    )
    check_token_balance(headers, body_dict, max_cost_for_model)

    if x_cashu := headers.get("x-cashu", None):
        return await upstream.handle_x_cashu(
            request, x_cashu, path, max_cost_for_model, model
        )

    elif auth := headers.get("authorization", None):
        key = await get_credit(auth, session)

    # Only pay for request if we have request body data (for completions endpoints)
    if body_dict:
        await pay_for_request(key, max_cost_for_model, session)

    # Prepare headers for upstream
    headers = upstream.prepare_headers(dict(request.headers))

    # Forward to upstream and handle response
    response = await upstream.forward_request(
        request,
        path,
        headers,
        body_bytes,
        key,
        max_cost_for_model,
        session,
        model,
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


def parse_request_body_json(request_body: bytes) -> dict[str, Any]:
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
                    "body_keys": list(request_body_dict.keys()),
                    "model": request_body_dict.get("model", "not_specified"),
                },
            )
        except json.JSONDecodeError as e:
            logger.error(
                "Invalid JSON in request body",
                extra={
                    "error": str(e),
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
