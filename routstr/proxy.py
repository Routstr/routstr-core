import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from sqlmodel import col, select

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
from .payment.models import Model, _row_to_model
from .upstream import UpstreamProvider, init_upstreams, resolve_model_alias

logger = get_logger(__name__)
proxy_router = APIRouter()

_upstreams: list[UpstreamProvider] = []
_model_instances: dict[str, Model] = {}  # All aliases -> Model
_provider_map: dict[str, UpstreamProvider] = {}  # All aliases -> Provider
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


def get_upstreams() -> list[UpstreamProvider]:
    """Get the initialized upstream providers.

    Returns:
        List of upstream provider instances
    """
    return _upstreams


def get_model_instance(model_id: str) -> Model | None:
    """Get Model instance by ID from global cache."""
    return _model_instances.get(model_id)


def get_provider_for_model(model_id: str) -> UpstreamProvider | None:
    """Get UpstreamProvider for model ID from global cache."""
    return _provider_map.get(model_id)


def get_unique_models() -> list[Model]:
    """Get list of unique models (no duplicates from aliases)."""
    return list(_unique_models.values())


async def refresh_model_maps() -> None:
    """Refresh global model and provider maps in-place."""
    global _model_instances, _provider_map, _unique_models

    model_instances: dict[str, Model] = {}
    provider_map: dict[str, UpstreamProvider] = {}
    unique_models: dict[str, Model] = {}
    openrouter: UpstreamProvider | None = None
    other_upstreams: list[UpstreamProvider] = []

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

        # Get all disabled model IDs from database to filter them out
        disabled_result = await session.exec(
            select(ModelRow.id).where(not col(ModelRow.enabled))
        )
        disabled_model_ids = {row for row in disabled_result.all()}

    for upstream in _upstreams:
        if upstream.base_url == "https://openrouter.ai/api/v1":
            openrouter = upstream
        else:
            other_upstreams.append(upstream)

    def get_base_model_id(model_id: str) -> str:
        """Get base model ID by removing provider prefix."""
        return model_id.split("/", 1)[1] if "/" in model_id else model_id

    def _alias_priority(alias: str, model: Model) -> int:
        """Rank how strong the mapping of alias->model is.

        Highest priority when alias exactly equals the model ID without provider prefix.
        Next when alias equals canonical slug without prefix. Otherwise lowest.
        """
        model_base = get_base_model_id(model.id)
        if model_base == alias:
            return 3
        if model.canonical_slug:
            canonical_base = get_base_model_id(model.canonical_slug)
            if canonical_base == alias:
                return 2
        return 1

    def _maybe_set_alias(alias: str, model: Model, provider: UpstreamProvider) -> None:
        existing = model_instances.get(alias)
        if not existing or _alias_priority(alias, model) > _alias_priority(
            alias, existing
        ):
            model_instances[alias] = model
            provider_map[alias] = provider

    if openrouter:
        for model in openrouter.get_cached_models():
            if model.enabled and model.id not in disabled_model_ids:
                if model.id in overrides_by_id:
                    override_row, provider_fee = overrides_by_id[model.id]
                    model_to_use = _row_to_model(
                        override_row, apply_provider_fee=True, provider_fee=provider_fee
                    )
                else:
                    model_to_use = model
                base_id = get_base_model_id(model_to_use.id)
                if base_id not in unique_models:
                    unique_model = model_to_use.copy(update={"id": base_id})
                    unique_models[base_id] = unique_model
                for alias in resolve_model_alias(
                    model_to_use.id, model_to_use.canonical_slug
                ):
                    _maybe_set_alias(alias, model_to_use, openrouter)

    for upstream in other_upstreams:
        upstream_prefix = getattr(upstream, "upstream_name", None)
        for model in upstream.get_cached_models():
            if model.enabled and model.id not in disabled_model_ids:
                if model.id in overrides_by_id:
                    override_row, provider_fee = overrides_by_id[model.id]
                    model_to_use = _row_to_model(
                        override_row, apply_provider_fee=True, provider_fee=provider_fee
                    )
                else:
                    model_to_use = model
                base_id = get_base_model_id(model_to_use.id)
                unique_model = model_to_use.copy(update={"id": base_id})
                unique_models[base_id] = unique_model

                aliases = resolve_model_alias(
                    model_to_use.id, model_to_use.canonical_slug
                )

                if upstream_prefix and "/" not in model_to_use.id:
                    prefixed_id = f"{upstream_prefix}/{model_to_use.id}"
                    if prefixed_id not in aliases:
                        aliases.append(prefixed_id)

                for alias in aliases:
                    _maybe_set_alias(alias, model_to_use, upstream)

    _model_instances = model_instances
    _provider_map = provider_map
    _unique_models = unique_models

    logger.debug(
        "Refreshed model maps",
        extra={
            "unique_model_count": len(_unique_models),
            "total_alias_count": len(_model_instances),
        },
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
    print(model_id)
    print(model_obj)

    upstream = get_provider_for_model(model_id)
    if not upstream:
        return create_error_response(
            "invalid_model",
            f"No provider found for model '{model_id}'",
            400,
            request=request,
        )

    print(upstream.upstream_name)

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
                raise HTTPException(
                    status_code=400,
                    detail={"error": "max_tokens must be an integer (without quotes)"},
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
