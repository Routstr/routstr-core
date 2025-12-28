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
from .core.settings import settings
from .payment.helpers import (
    calculate_discounted_max_cost,
    check_token_balance,
    create_error_response,
    get_max_cost_for_model,
)
from .payment.models import Model
from .upstream import BaseUpstreamProvider
from .upstream.helpers import init_upstreams
from .wallet import deserialize_token_from_string

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
    return _model_instances.get(model_id.lower())


def get_provider_for_model(model_id: str) -> BaseUpstreamProvider | None:
    """Get UpstreamProvider for model ID from global cache."""
    return _provider_map.get(model_id.lower())


def get_unique_models() -> list[Model]:
    """Get list of unique models (no duplicates from aliases)."""
    return list(_unique_models.values())


async def refresh_model_maps() -> None:
    """Refresh global model and provider maps using the cost-based algorithm."""
    from sqlalchemy.orm import selectinload

    global _model_instances, _provider_map, _unique_models

    async with create_session() as session:
        # Fetch all providers with their models in a single logical operation
        query = select(UpstreamProviderRow).options(
            selectinload(UpstreamProviderRow.models)  # type: ignore
        )
        result = await session.exec(query)
        provider_rows = result.all()

    overrides_by_id: dict[str, tuple[ModelRow, float]] = {}
    disabled_model_ids: set[str] = set()

    for provider in provider_rows:
        for model in provider.models:
            if model.enabled:
                overrides_by_id[model.id] = (model, provider.provider_fee)
            else:
                disabled_model_ids.add(model.id)

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

    is_responses_api = path.startswith("v1/responses") or path.startswith("responses")
    request_body = await request.body()
    request_body_dict = parse_request_body_json(request_body, path)

    if is_responses_api:
        model_id = extract_model_from_responses_request(request_body_dict)
    else:
        model_id = request_body_dict.get("model", "unknown")

    if "https://testnut.cashu.space" in settings.cashu_mints:
        try:
            token_str = None
            if x_cashu_header := headers.get("x-cashu"):
                token_str = x_cashu_header
            elif auth_header := headers.get("authorization"):
                parts = auth_header.split(" ")
                if len(parts) > 1 and not parts[1].startswith("sk-"):
                    token_str = parts[1]

            if token_str:
                token_obj = deserialize_token_from_string(token_str)
                if token_obj.mint == "https://testnut.cashu.space":
                    model_id = "mock/gpt-420-mock"
                    request_body_dict["model"] = model_id
        except Exception:
            pass

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
        if is_responses_api:
            return await upstream.handle_x_cashu_responses(
                request, x_cashu, path, max_cost_for_model, model_obj
            )
        else:
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
        headers = upstream.prepare_headers(dict(request.headers))
        return await upstream.forward_get_request(request, path, headers)

    if request_body_dict:
        await pay_for_request(key, max_cost_for_model, session)

    headers = upstream.prepare_headers(dict(request.headers))

    if is_responses_api:
        response = await upstream.forward_responses_request(
            request,
            path,
            headers,
            request_body,
            key,
            max_cost_for_model,
            session,
            model_obj,
        )
    else:
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


def extract_model_from_responses_request(request_body_dict: dict[str, Any]) -> str:
    if model := request_body_dict.get("model"):
        return model

    if input_data := request_body_dict.get("input"):
        if isinstance(input_data, dict) and (model := input_data.get("model")):
            return model

    if request_body_dict.get("messages"):
        return "unknown"

    logger.warning(
        "No model found in Responses API request",
        extra={"body_keys": list(request_body_dict.keys())}
    )
    return "unknown"


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
