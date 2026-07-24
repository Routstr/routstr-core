import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from sqlmodel import select

from .algorithm import create_model_mappings
from .auth import (
    ReservationSnapshot,
    get_reservation_snapshot,
    pay_for_request,
    revert_pay_for_request,
    validate_bearer_key,
)
from .core import get_logger
from .core.db import (
    ApiKey,
    AsyncSession,
    ModelRow,
    UpstreamProviderRow,
    create_session,
    get_session,
)
from .core.exceptions import UpstreamError
from .core.not_found import build_not_found_response
from .core.settings import settings
from .payment.helpers import (
    calculate_discounted_max_cost,
    check_token_balance,
    create_error_response,
    create_upstream_error_response,
    get_max_cost_for_model,
)
from .payment.models import Model
from .upstream import BaseUpstreamProvider
from .upstream.ehbp import forward_ehbp_request, forward_ehbp_x_cashu_request
from .upstream.helpers import init_upstreams
from .upstream.request_correction import correct_request, extract_error_message

logger = get_logger(__name__)
proxy_router = APIRouter()

_upstreams: list[BaseUpstreamProvider] = []
_provider_map: dict[
    str, list[tuple[Model, BaseUpstreamProvider]]
] = {}  # All aliases -> sorted [(candidate Model, its Provider)]
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


def get_candidates(
    model_id: str,
) -> list[tuple[Model, BaseUpstreamProvider]] | None:
    """Get the sorted (model, provider) candidate list for a model ID.

    Each provider is paired with its own model for the alias, so routing can
    forward and bill the candidate that actually serves. Version suffixes
    (e.g. ``-20251222``) are stripped as a retry when the exact ID is
    unknown, since upstreams may return a specific version of a base model
    we track.
    """
    if not model_id:
        return None

    model_id_lower = model_id.lower()
    if candidates := _provider_map.get(model_id_lower):
        return candidates

    import re

    base_model_id = re.sub(r"-\d{8}$", "", model_id_lower)
    if base_model_id != model_id_lower:
        if candidates := _provider_map.get(base_model_id):
            return candidates

    return None


def get_model_instance(model_id: str) -> Model | None:
    """Get the best-ranked Model instance for a model ID."""
    candidates = get_candidates(model_id)
    return candidates[0][0] if candidates else None


def get_provider_for_model(model_id: str) -> list[BaseUpstreamProvider] | None:
    """Get the sorted UpstreamProvider list for a model ID."""
    candidates = get_candidates(model_id)
    return [provider for _, provider in candidates] if candidates else None


def get_unique_models() -> list[Model]:
    """Get list of unique models (no duplicates from aliases)."""
    return list(_unique_models.values())


def _is_tinfoil_attestation_path(path: str) -> bool:
    """Return True for exact Tinfoil attestation routes, with optional slash."""
    return path in {
        "attestation",
        "attestation/",
        "tee/attestation",
        "tee/attestation/",
    }


def _select_unauthenticated_get_upstreams(
    path: str, upstreams: list[BaseUpstreamProvider]
) -> list[BaseUpstreamProvider]:
    """Select upstream candidates for unauthenticated GET bypass paths.

    Tinfoil attestation endpoints are provider-specific. Trying every enabled
    upstream can return an unrelated provider's 404 before Tinfoil is reached,
    so route those paths only to Tinfoil providers.
    """
    if _is_tinfoil_attestation_path(path):
        return [
            upstream
            for upstream in upstreams
            if getattr(upstream, "provider_type", None) == "tinfoil"
        ]
    return upstreams


async def refresh_model_maps() -> None:
    """Refresh global model and provider maps using the cost-based algorithm."""
    from sqlalchemy.orm import selectinload

    global _provider_map, _unique_models

    async with create_session() as session:
        # Fetch all providers with their models in a single logical operation
        query = select(UpstreamProviderRow).options(
            selectinload(UpstreamProviderRow.models)  # type: ignore
        )
        result = await session.exec(query)
        provider_rows = result.all()

    overrides_by_key: dict[tuple[str, int], tuple[ModelRow, float]] = {}
    disabled_model_keys: set[tuple[str, int]] = set()

    for provider in provider_rows:
        if not provider.enabled:
            continue
        for model in provider.models:
            model_key = (model.id.lower(), model.upstream_provider_id)
            if model.enabled:
                overrides_by_key[model_key] = (model, provider.provider_fee)
            else:
                disabled_model_keys.add(model_key)

    _, _provider_map, _unique_models = create_model_mappings(
        upstreams=_upstreams,
        overrides_by_key=overrides_by_key,
        disabled_model_keys=disabled_model_keys,
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


_API_PATH_PREFIXES = (
    "v1/",
    "responses",
    "chat/",
    "completions",
    "models",
    "embeddings",
    "audio/",
    "images/",
    "moderations",
    "providers",
    "tee/",
    "attestation",
)


@proxy_router.api_route("/{path:path}", methods=["GET", "POST"], response_model=None)
async def proxy(
    request: Request, path: str, session: AsyncSession = Depends(get_session)
) -> Response | StreamingResponse:
    # GET requests must hit a known API prefix; otherwise return a 404 (HTML
    # for browsers, JSON for API clients). POST requests are always forwarded
    # so that OpenAI-style endpoints work with or without the `v1/` prefix
    # (e.g. `/chat/completions` as well as `/v1/chat/completions`).
    if request.method == "GET" and not path.startswith(_API_PATH_PREFIXES):
        return build_not_found_response(request, path)

    headers = dict(request.headers)

    is_responses_api = path.startswith("v1/responses") or path.startswith("responses")
    request_body = await request.body()

    # EHBP (Encrypted HTTP Body Protocol) requests carry an Ehbp-Encapsulated-Key
    # header and a binary HPKE-sealed body. The proxy cannot parse the body to
    # extract the model id, so the SDK sends it in X-Routstr-Model. Forward the
    # raw encrypted body to the upstream's /private/ endpoint and stream the
    # encrypted response back untouched — the SDK's SecureClient decrypts it.
    is_ehbp = "ehbp-encapsulated-key" in headers
    if is_ehbp:
        request_body_dict = {}
        model_id = headers.get("x-routstr-model", "")
        if not model_id:
            return create_error_response(
                "invalid_request",
                "EHBP request missing X-Routstr-Model header",
                400,
                request=request,
            )
    else:
        request_body_dict = parse_request_body_json(request_body, path)
        if is_responses_api:
            model_id = extract_model_from_responses_request(request_body_dict)
        else:
            model_id = request_body_dict.get("model", "unknown")

    # Exact Tinfoil attestation GET routes don't map to models — forward
    # without model/cost/auth lookups. Do not prefix-match here: paths such as
    # /attestationjunk must continue through normal authentication.
    if request.method == "GET" and _is_tinfoil_attestation_path(path):
        selected_upstreams = _select_unauthenticated_get_upstreams(path, _upstreams)
        if not selected_upstreams:
            return create_error_response(
                "upstream_error",
                "No upstream available for unauthenticated GET path",
                502,
                request=request,
            )

        last_error_response = None
        for i, upstream in enumerate(selected_upstreams):
            try:
                headers = upstream.prepare_headers(dict(request.headers))
                response = await upstream.forward_get_request(request, path, headers)
                if (
                    response.status_code in [502, 429]
                    and i < len(selected_upstreams) - 1
                ):
                    logger.warning(
                        "Upstream %s returned %s for unauthenticated GET %s, trying next",
                        upstream.provider_type,
                        response.status_code,
                        path,
                    )
                    continue
                return response
            except UpstreamError as e:
                logger.warning(
                    "Upstream %s failed for unauthenticated GET %s: %s",
                    upstream.provider_type,
                    path,
                    e,
                )
                if i == len(selected_upstreams) - 1:
                    last_error_response = create_upstream_error_response(e, request)
                continue
        return last_error_response or create_error_response(
            "upstream_error", "All upstreams failed", 502, request=request
        )

    candidates = get_candidates(model_id)

    if not candidates:
        return create_error_response(
            "invalid_model", f"Model '{model_id}' not found", 400, request=request
        )

    if is_ehbp:
        candidates = [
            (model, upstream)
            for model, upstream in candidates
            if upstream.supports_ehbp
        ]
        if not candidates:
            return create_error_response(
                "unsupported_request",
                f"No EHBP-capable provider found for model '{model_id}'",
                400,
                request=request,
            )

    # Reserve/max-cost checks use the best-ranked candidate; the failover loop
    # below rebinds (model_obj, upstream) per candidate so forwarding and
    # settlement always use the model of the provider actually being tried.
    model_obj = candidates[0][0]

    _max_cost_for_model = await get_max_cost_for_model(
        model=model_id, session=session, model_obj=model_obj
    )
    max_cost_for_model = await calculate_discounted_max_cost(
        _max_cost_for_model, request_body_dict, model_obj=model_obj
    )
    # Ensure max_cost_for_model is at least the minimum allowed request cost
    max_cost_for_model = max(max_cost_for_model, settings.min_request_msat)

    check_token_balance(headers, request_body_dict, max_cost_for_model)

    if x_cashu := headers.get("x-cashu", None):
        last_error = None
        for i, (model_obj, upstream) in enumerate(candidates):
            try:
                if is_ehbp:
                    if not upstream.supports_ehbp:
                        logger.warning(
                            "Upstream %s does not support EHBP for model=%s",
                            upstream.provider_type,
                            model_id,
                        )
                        continue
                    return await forward_ehbp_x_cashu_request(
                        request=request,
                        x_cashu_token=x_cashu,
                        path=path,
                        max_cost_for_model=max_cost_for_model,
                        model_obj=model_obj,
                        upstream=upstream,
                    )
                elif is_responses_api:
                    return await upstream.handle_x_cashu_responses(
                        request, x_cashu, path, max_cost_for_model, model_obj
                    )
                else:
                    return await upstream.handle_x_cashu(
                        request, x_cashu, path, max_cost_for_model, model_obj
                    )
            except UpstreamError as e:
                logger.warning(
                    "Upstream %s failed (x-cashu) for model=%s: %s",
                    upstream.provider_type,
                    model_id,
                    e,
                    extra={
                        "provider": upstream.provider_type,
                        "model": model_id,
                        "status_code": e.status_code,
                    },
                )
                if i == len(candidates) - 1:
                    last_error = e
                continue

        if last_error is not None:
            return create_upstream_error_response(last_error, request)
        return create_error_response(
            "upstream_error", "All upstreams failed", 502, request=request
        )

    elif auth := headers.get("authorization", None):
        key = await get_bearer_token_key(
            headers, path, session, auth, max_cost_for_model, model_id
        )

    else:
        if request.method not in ["GET"]:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {"type": "invalid_request_error", "code": "unauthorized"}
                },
            )

        logger.debug("Processing unauthenticated GET request", extra={"path": path})

        last_error_response = None
        for i, (_, upstream) in enumerate(candidates):
            try:
                headers = upstream.prepare_headers(dict(request.headers))
                response = await upstream.forward_get_request(request, path, headers)

                if response.status_code in [502, 429] and i < len(candidates) - 1:
                    error_message = ""
                    try:
                        if hasattr(response, "body"):
                            body_bytes = response.body
                            data = json.loads(body_bytes)
                            if "error" in data:
                                error_data = data["error"]
                                if isinstance(error_data, dict):
                                    error_message = error_data.get("message", "")
                                elif isinstance(error_data, str):
                                    error_message = error_data
                    except Exception:
                        pass

                    await upstream.on_upstream_error_redirect(
                        response.status_code, error_message
                    )

                    logger.warning(
                        f"Upstream {upstream.provider_type} returned {response.status_code} (GET), trying next provider",
                        extra={
                            "status_code": response.status_code,
                            "upstream": upstream.provider_type,
                        },
                    )
                    continue
                return response
            except UpstreamError as e:
                logger.warning(f"Upstream {upstream.provider_type} failed (GET): {e}")
                if i == len(candidates) - 1:
                    last_error_response = create_upstream_error_response(e, request)
                continue
        return last_error_response or create_error_response(
            "upstream_error", "All upstreams failed", 502, request=request
        )

    reservation_snapshot: ReservationSnapshot | None = None
    if is_ehbp or request_body_dict:
        await pay_for_request(key, max_cost_for_model, session)
        reservation_snapshot = await get_reservation_snapshot(key, session)

    # Tracks request params already removed in response to upstream rejections,
    # shared across providers so a stripped param stays stripped on failover and
    # the reactive retry can never loop unboundedly.
    already_stripped: set[str] = set()

    for i, (model_obj, upstream) in enumerate(candidates):
        if i > 0 and request_body_dict:
            # The reservation was sized to the previous candidate's envelope;
            # settlement bills the serving candidate, so a pricier fallback
            # must be re-reserved at its own max cost before it is tried. A
            # candidate whose envelope the key cannot cover is rejected, just
            # as it would be had it been ranked first.
            candidate_max = await get_max_cost_for_model(
                model=model_id, session=session, model_obj=model_obj
            )
            candidate_max = await calculate_discounted_max_cost(
                candidate_max, request_body_dict, model_obj=model_obj
            )
            candidate_max = max(candidate_max, settings.min_request_msat)
            if candidate_max > max_cost_for_model:
                await revert_pay_for_request(
                    key, session, max_cost_for_model, reservation_snapshot
                )
                try:
                    await pay_for_request(key, candidate_max, session)
                except HTTPException:
                    if i == len(candidates) - 1:
                        raise
                    await pay_for_request(key, max_cost_for_model, session)
                    reservation_snapshot = await get_reservation_snapshot(key, session)
                    continue
                reservation_snapshot = await get_reservation_snapshot(key, session)
                max_cost_for_model = candidate_max

        headers = upstream.prepare_headers(dict(request.headers))

        try:
            while True:
                try:
                    if is_ehbp:
                        if not upstream.supports_ehbp:
                            logger.warning(
                                "Upstream %s does not support EHBP for model=%s",
                                upstream.provider_type,
                                model_id,
                            )
                            raise UpstreamError(
                                f"Provider {upstream.provider_type} does not support EHBP",
                                status_code=400,
                            )
                        response = await forward_ehbp_request(
                            request=request,
                            path=path,
                            headers=headers,
                            request_body=request_body,
                            upstream=upstream,
                            key=key,
                            max_cost_for_model=max_cost_for_model,
                            session=session,
                            model_obj=model_obj,
                            reservation_snapshot=reservation_snapshot,
                        )
                    elif is_responses_api:
                        response = await upstream.forward_responses_request(
                            request,
                            path,
                            headers,
                            request_body,
                            key,
                            max_cost_for_model,
                            session,
                            model_obj,
                            reservation_snapshot,
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
                            reservation_snapshot,
                        )
                except UpstreamError:
                    # Let the outer UpstreamError handler manage retry/revert
                    raise
                except Exception as e:
                    # Unexpected error (not an upstream failure) — revert and propagate
                    logger.error(
                        "Unexpected error in upstream request, reverting payment",
                        extra={
                            "error": str(e),
                            "error_type": type(e).__name__,
                            "path": path,
                            "key_hash": key.hashed_key[:8] + "...",
                            "max_cost_for_model": max_cost_for_model,
                        },
                    )
                    await revert_pay_for_request(
                        key, session, max_cost_for_model, reservation_snapshot
                    )
                    raise

                # Reactive recovery: some models reject one specific request
                # param (e.g. newer Anthropic models deprecating `temperature`).
                # When the upstream 400s naming such a param, strip it from the
                # body and retry the SAME upstream. ``already_stripped`` bounds
                # this to one retry per distinct param so it always terminates.
                if response.status_code == 400 and not is_ehbp:
                    correction = correct_request(
                        request_body,
                        extract_error_message(response),
                        already_stripped,
                    )
                    if correction is not None:
                        request_body, bad_param = correction.body, correction.label
                        already_stripped.add(bad_param)
                        logger.warning(
                            "Upstream %s rejected param '%s' for model=%s; "
                            "stripping and retrying same upstream",
                            upstream.provider_type,
                            bad_param,
                            model_id,
                            extra={
                                "provider": upstream.provider_type,
                                "model": model_id,
                                "stripped_param": bad_param,
                                "path": path,
                            },
                        )
                        continue
                break

            if response.status_code != 200:
                # Check if we should retry (502 Upstream Error or 429 Rate Limit)
                should_retry = response.status_code in [502, 429, 400, 401, 403, 404]
                if should_retry and i < len(candidates) - 1:
                    error_message = ""
                    try:
                        if hasattr(response, "body"):
                            body_bytes = response.body
                            data = json.loads(body_bytes)
                            if "error" in data:
                                error_data = data["error"]
                                if isinstance(error_data, dict):
                                    error_message = error_data.get("message", "")
                                elif isinstance(error_data, str):
                                    error_message = error_data
                    except Exception:
                        pass

                    await upstream.on_upstream_error_redirect(
                        response.status_code, error_message
                    )

                    logger.warning(
                        "Upstream %s returned %s for model=%s, trying next provider",
                        upstream.provider_type,
                        response.status_code,
                        model_id,
                        extra={
                            "status_code": response.status_code,
                            "provider": upstream.provider_type,
                            "model": model_id,
                        },
                    )
                    continue

                # 4xx error (user error), or other non-retryable error, or last provider failed
                await revert_pay_for_request(
                    key, session, max_cost_for_model, reservation_snapshot
                )
                logger.warning(
                    "Upstream request failed, revert payment "
                    "(provider=%s model=%s status=%s path=%s)",
                    upstream.provider_type,
                    model_id,
                    response.status_code,
                    path,
                    extra={
                        "status_code": response.status_code,
                        "path": path,
                        "provider": upstream.provider_type,
                        "model": model_id,
                        "key_hash": key.hashed_key[:8] + "...",
                        "key_balance": key.balance,
                        "max_cost_for_model": max_cost_for_model,
                    },
                )
                return response

            return response

        except asyncio.CancelledError:
            logger.warning(
                "Client disconnected mid-request, reverting reservation",
                extra={
                    "path": path,
                    "model": model_id,
                    "key_hash": key.hashed_key[:8] + "...",
                    "max_cost_for_model": max_cost_for_model,
                },
            )
            # The cancellation has been caught, so complete exact cleanup in
            # this task before the request-scoped session can be torn down.
            await revert_pay_for_request(
                key, session, max_cost_for_model, reservation_snapshot
            )
            raise

        except UpstreamError as e:
            logger.warning(
                "Upstream %s failed for model=%s: %s",
                upstream.provider_type,
                model_id,
                e,
                extra={
                    "provider": upstream.provider_type,
                    "model": model_id,
                    "status_code": e.status_code,
                    "retry": i < len(candidates) - 1,
                },
            )

            # If this was the last provider
            if i == len(candidates) - 1:
                await revert_pay_for_request(
                    key, session, max_cost_for_model, reservation_snapshot
                )
                return create_upstream_error_response(e, request)

            # Otherwise loop continues to next provider
            continue

    # Should not be reached given logic above
    return create_error_response(
        "upstream_error", "All upstreams failed", 502, request=request
    )


async def get_bearer_token_key(
    headers: dict,
    path: str,
    session: AsyncSession,
    auth: str,
    min_cost: int = 0,
    model_id: str = "unknown",
) -> ApiKey:
    """Handle bearer token authentication proxy requests."""
    parts = auth.split()
    bearer_key = parts[1] if len(parts) > 1 and parts[0].lower() == "bearer" else ""
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
            "min_cost": min_cost,
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
            min_cost=min_cost,
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
        key_preview = bearer_key[:20] + "..." if len(bearer_key) > 20 else bearer_key
        logger.error(
            f"Bearer token validation failed: {type(e).__name__}: {e} path={path} model={model_id!r} min_cost={min_cost} key={key_preview!r}",
            extra={
                "error": str(e),
                "error_type": type(e).__name__,
                "path": path,
                "model_id": model_id,
                "min_cost_msat": min_cost,
                "bearer_key_preview": key_preview,
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
        extra={"body_keys": list(request_body_dict.keys())},
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
