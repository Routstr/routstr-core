import asyncio
import inspect
import json
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from sqlmodel import select

from .algorithm import create_model_mappings
from .auth import (
    ReservationSnapshot,
    pay_for_request,
    revert_pay_for_request,
    validate_bearer_key,
)
from .core import get_logger
from .core.db import (
    ApiKey,
    AsyncSession,
    ModelPathRow,
    ModelRow,
    UpstreamProviderRow,
    create_session,
)
from .core.error_scope import (
    ERROR_SCOPE_HEADER,
    ERROR_SCOPE_UPSTREAM,
    UPSTREAM_ERROR_STATUS,
    UPSTREAM_UNAVAILABLE,
)
from .core.exceptions import UpstreamError
from .core.middleware import mark
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
from .upstream.cooldown import (
    candidate_model_identity,
    is_cooling_down,
    provider_identity,
    record_failure,
)
from .upstream.ehbp import forward_ehbp_request, forward_ehbp_x_cashu_request
from .upstream.helpers import init_upstreams
from .upstream.model_paths import (
    ModelPathSelector,
    decode_model_path,
    is_openrouter_base_url,
    price_pinned_endpoint,
    public_model_id,
    public_provider_url,
)
from .upstream.request_correction import correct_request, extract_error_message

logger = get_logger(__name__)

MODEL_PATH_HEADER = "x-routstr-model-path"
proxy_router = APIRouter()

_upstreams: list[BaseUpstreamProvider] = []
_provider_map: dict[
    str, list[tuple[Model, BaseUpstreamProvider]]
] = {}  # All aliases -> sorted [(candidate Model, its Provider)]
_unique_models: dict[str, Model] = {}  # Unique model.id -> Model (no duplicates)


async def _finish_read_transaction(session: AsyncSession) -> None:
    """Release a read transaction without assuming a particular session mock."""
    commit_result = session.commit()
    if inspect.isawaitable(commit_result):
        await commit_result


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

    base_model_id = re.sub(r"-\d{8}$", "", model_id_lower)
    if base_model_id != model_id_lower:
        if candidates := _provider_map.get(base_model_id):
            return candidates

    return None


def _model_ids_match(requested: str, selected: str) -> bool:
    if requested.lower() == selected.lower():
        return True
    return public_model_id(requested).lower() == public_model_id(selected).lower()


def _candidate_for_selector(
    selector: ModelPathSelector,
    candidates: list[tuple[Model, BaseUpstreamProvider]],
) -> tuple[Model, BaseUpstreamProvider] | None:
    """Resolve a route selector to one candidate.

    ``candidates`` is ranked by cost, so the first URL match is the cheapest
    provider configured against that URL. A selector still carrying a legacy
    ``provider-id`` keeps pinning that exact provider instead.
    """
    for model_obj, upstream in candidates:
        if public_provider_url(upstream.base_url) != selector.base_url:
            continue
        if selector.provider_id is not None and upstream.db_id != selector.provider_id:
            continue
        return model_obj, upstream
    return None


async def _price_pinned_endpoint(
    session: AsyncSession,
    selector: ModelPathSelector,
    model_obj: Model,
    upstream: BaseUpstreamProvider,
) -> Model:
    """Reprice ``model_obj`` with the pinned endpoint's own rates.

    An OpenRouter endpoint can cost more than the model's default listing, and
    ``/v1/models/paths`` quotes that endpoint's price, so the reservation and
    token billing must use it too. Without a stored path row or a sats price
    the request keeps the model's default pricing.
    """
    from .payment import price as price_module

    sats_to_usd = price_module.SATS_USD_PRICE
    if upstream.db_id is None or not sats_to_usd:
        return model_obj
    rows = (
        await session.exec(
            select(ModelPathRow).where(
                ModelPathRow.upstream_provider_id == upstream.db_id,
                ModelPathRow.endpoint_tag == selector.endpoint_tag,
            )
        )
    ).all()
    row = next(
        (r for r in rows if _model_ids_match(r.model_id, selector.model_id)), None
    )
    if row is None:
        logger.warning(
            "No stored path for pinned endpoint; billing the model's default pricing",
            extra={"model": selector.model_id, "endpoint": selector.endpoint_tag},
        )
        return model_obj
    return await price_pinned_endpoint(
        session, model_obj, row, upstream.provider_fee, sats_to_usd
    )


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

    # Keep model-path discovery in sync with admin mutations: disabling or
    # deleting a provider must stop advertising its paths immediately rather
    # than after the next timed refresh.
    from .upstream.model_paths import prune_model_paths_for_inactive_providers

    try:
        await prune_model_paths_for_inactive_providers()
    except Exception as e:  # noqa: BLE001 - discovery sync must not break routing
        logger.warning(
            "Failed to prune model paths for inactive providers",
            extra={"error": str(e), "error_type": type(e).__name__},
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


# Canonical endpoints this proxy will forward, keyed by the path with any
# leading "v1/" and trailing slash removed, mapped to the methods allowed on
# each. The provider credential is attached during forwarding, so endpoint
# permission has to come from this table rather than from the client-supplied
# path: an upstream's key-management, organization, or billing routes live
# under the same origin and must never be reachable through the proxy.
_ALLOWED_ENDPOINTS: dict[str, frozenset[str]] = {
    "chat/completions": frozenset({"POST"}),
    "completions": frozenset({"POST"}),
    "responses": frozenset({"POST"}),
    "messages": frozenset({"POST"}),
    # Anthropic token-counting subroute; the proxy's allowlist is exact, so the
    # "messages" entry above does not carry it. Clients (Claude Code, the
    # Anthropic SDKs) call it before every request.
    "messages/count_tokens": frozenset({"POST"}),
    "embeddings": frozenset({"POST"}),
    # TypeSafe System One decision endpoint: POST {state, model, questions}
    # -> {answers, usage}. Non-streaming, JSON in/out; billed from the
    # response's usage exactly like embeddings.
    "systemone": frozenset({"POST"}),
    "models": frozenset({"GET"}),
    "attestation": frozenset({"GET"}),
    "tee/attestation": frozenset({"GET"}),
}

_ALLOWED_METHODS = frozenset({"GET", "POST"})


def _canonical_api_path(path: str) -> str:
    """Reduce a request path to its allowlist key.

    OpenAI-style clients reach the same endpoint with or without the ``v1/``
    prefix and with or without a trailing slash, so both spellings collapse to
    one key. Callers must screen the path with
    :func:`_is_ambiguously_spelled_path` first — this function assumes the path
    has no dot segments, empty segments, or encoded separators left to resolve.
    """
    core = path[:-1] if path.endswith("/") else path
    if core.startswith("v1/"):
        core = core[len("v1/") :]
    return core


def _parse_extra_allowed_endpoints(raw: str) -> dict[str, frozenset[str]]:
    """Parse operator-configured additions to the endpoint allowlist.

    Deployments whose provider exposes an endpoint outside the canonical set
    opt in explicitly with ``PROXY_EXTRA_ALLOWED_PATHS``, a comma-separated
    list of ``METHOD:path`` pairs (e.g. ``POST:v1/rerank,GET:batches``). Every
    entry must name one concrete method and one unambiguous path; wildcards
    and bare prefixes are deliberately unsupported, so widening the proxy's
    reach is always a per-endpoint decision. Malformed entries are dropped
    with a warning rather than silently widening or narrowing the surface.
    """
    extra: dict[str, frozenset[str]] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        method, separator, endpoint = entry.partition(":")
        method = method.strip().upper()
        endpoint = endpoint.strip()
        if not separator or method not in _ALLOWED_METHODS or not endpoint:
            logger.warning(
                "Ignoring malformed PROXY_EXTRA_ALLOWED_PATHS entry",
                extra={"entry": entry},
            )
            continue
        if _is_ambiguously_spelled_path(endpoint):
            logger.warning(
                "Ignoring ambiguously spelled PROXY_EXTRA_ALLOWED_PATHS entry",
                extra={"entry": entry},
            )
            continue
        if any(character in endpoint for character in "*?["):
            # Refuse glob syntax outright. Kept as a literal endpoint name it
            # would never match a real request, so the operator would think
            # they had widened the proxy when they had not.
            logger.warning(
                "Ignoring wildcard PROXY_EXTRA_ALLOWED_PATHS entry; "
                "list each endpoint explicitly",
                extra={"entry": entry},
            )
            continue
        key = _canonical_api_path(endpoint)
        extra[key] = extra.get(key, frozenset()) | {method}
    return extra


def _is_ambiguously_spelled_path(path: str) -> bool:
    """Reject paths whose spelling could resolve somewhere the allowlist did not.

    ``{path:path}`` arrives percent-decoded, so a client that sent ``%2e%2e`` or
    ``%2f`` shows up here as ``..`` / ``/``. Dot segments, backslashes, duplicate
    or leading separators, NUL bytes, and any residual encoded separator are
    treated as unsafe: they let a caller walk off the canonical API surface (and
    onto a sensitive upstream endpoint) even though the literal prefix check
    would pass. Reject rather than trying to rewrite the path.
    """
    if not path or path != path.strip() or path.startswith("/"):
        return True
    if "\x00" in path or "\\" in path:
        return True
    # A single trailing slash is canonical (e.g. "attestation/"); ignore it,
    # then no remaining segment may be empty (covers "//") or a dot segment.
    core = path[:-1] if path.endswith("/") else path
    if any(segment in ("", ".", "..") for segment in core.split("/")):
        return True
    lowered = path.lower()
    return "%2e" in lowered or "%2f" in lowered or "%5c" in lowered


_EXTRA_ALLOWED_ENDPOINTS = _parse_extra_allowed_endpoints(
    settings.proxy_extra_allowed_paths
)


def _allowed_methods_for(endpoint: str) -> frozenset[str]:
    """Return the methods allowed on a canonical endpoint, empty if unknown."""
    methods = _ALLOWED_ENDPOINTS.get(endpoint, frozenset())
    methods |= _EXTRA_ALLOWED_ENDPOINTS.get(endpoint, frozenset())
    return methods


def _forwarding_allowed(path: str, method: str) -> bool:
    """Gate which method/path pairs may reach an upstream at all.

    The provider credential is attached during forwarding, so an unknown
    endpoint must never be forwarded on the caller's say-so. The path is
    reduced to its canonical form and looked up in the endpoint table; there is
    no prefix match, so a known prefix no longer carries an unknown endpoint
    (``v1/organization/api_keys`` is rejected even though ``v1/`` is familiar).

    EHBP requests are gated by the same table. Their body is opaque to the
    proxy, which is a reason to constrain the destination more tightly, not to
    trust the caller's path: the encrypted contract covers the body, never the
    endpoint the credential is spent against.
    """
    if method not in _ALLOWED_METHODS:
        return False
    return method in _allowed_methods_for(_canonical_api_path(path))


# Gateway conditions a retry usually clears. 500 is excluded: as likely to be a
# deterministic rejection that fails identically on the next attempt.
_RETRYABLE_UPSTREAM_5XX = frozenset({502, 503, 504})
_UPSTREAM_5XX_RETRY_BACKOFF_SECONDS = 0.5


def _counts_toward_cooldown(status_code: int) -> bool:
    """Provider faults and timeouts only — not client errors or rate limits."""
    return status_code >= 500 or status_code == UPSTREAM_ERROR_STATUS


def _upstream_response_failure(response: Response) -> bool:
    return (
        _counts_toward_cooldown(response.status_code)
        and response.headers.get(ERROR_SCOPE_HEADER) == ERROR_SCOPE_UPSTREAM
    )


def _attribute_request(
    request: Request, model_obj: Model, upstream: BaseUpstreamProvider
) -> None:
    """Attribute the completion log line to the candidate being tried.

    Uses the provider's model id rather than the requested alias, so aliases
    and cross-provider spellings resolve to the model that was forwarded.
    """
    if model_obj.id:
        request.state.model = model_obj.id
    request.state.provider = upstream.provider_type


class _BodyLimitExceeded(Exception):
    """The client body is larger than ``max_request_body_bytes``."""


async def _read_bounded_body(request: Request) -> bytes | Response:
    """Read the request body under a size and time bound.

    Returns the body, or the error response to send instead. Both bounds run
    before any authentication or DB work, so an oversized or slowly uploaded
    body cannot occupy the request for longer than the timeout.
    """
    max_bytes = settings.max_request_body_bytes
    timeout = settings.request_body_timeout_seconds

    async def read() -> bytes:
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > max_bytes:
            raise _BodyLimitExceeded
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            # Chunked uploads declare no length, so the cap is enforced here.
            if len(body) > max_bytes:
                raise _BodyLimitExceeded
        return bytes(body)

    try:
        body = await asyncio.wait_for(read(), timeout)
    except _BodyLimitExceeded:
        error_type, message, status = (
            "invalid_request",
            f"Request body exceeds the {max_bytes} byte limit",
            413,
        )
    except asyncio.TimeoutError:
        error_type, message, status = (
            "timeout",
            f"Request body not received within {timeout} seconds",
            408,
        )
    else:
        # Draining the stream leaves Starlette unable to serve a second read.
        # Cache the body so later readers (EHBP forwarding, upstream stream
        # passthrough) get it instead of "Stream consumed".
        request._body = body
        return body
    return create_error_response(error_type, message, status, request=request)


@proxy_router.api_route("/{path:path}", methods=["GET", "POST"], response_model=None)
async def proxy(request: Request, path: str) -> Response | StreamingResponse:
    """Run proxy setup in a short request session, never across response streaming."""
    # Read the body before opening a session: a slow uploader must not hold a
    # DB connection while its request trickles in.
    request_body = await _read_bounded_body(request)
    if isinstance(request_body, Response):
        return request_body
    mark(request, "body_read")

    async with create_session() as session:
        try:
            return await _proxy(request, path, session, request_body)
        finally:
            # Close explicitly so a long stream cannot retain DB resources
            # while its response body is being sent.
            close_result = session.close()
            if inspect.isawaitable(close_result):
                await close_result


async def _proxy(
    request: Request, path: str, session: AsyncSession, request_body: bytes
) -> Response | StreamingResponse:
    # Screen the path before any routing decision: reject ambiguous spellings,
    # then require a known API prefix so nothing unknown is forwarded with the
    # provider credential attached.
    if _is_ambiguously_spelled_path(path):
        return build_not_found_response(request, path)

    headers = dict(request.headers)
    is_ehbp = "ehbp-encapsulated-key" in headers

    if not _forwarding_allowed(path, request.method):
        return build_not_found_response(request, path)

    is_responses_api = path.startswith("v1/responses") or path.startswith("responses")

    # EHBP (Encrypted HTTP Body Protocol) requests carry an Ehbp-Encapsulated-Key
    # header and a binary HPKE-sealed body. The proxy cannot parse the body to
    # extract the model id, so the SDK sends it in X-Routstr-Model. Forward the
    # raw encrypted body to the upstream's /private/ endpoint and stream the
    # encrypted response back untouched — the SDK's SecureClient decrypts it.
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

    # Set before routing so the completion log is attributed even when the
    # request fails before an upstream is chosen (400/401/402). "unknown" is
    # the no-model sentinel, not a model.
    if isinstance(model_id, str) and model_id and model_id != "unknown":
        request.state.model = model_id

    # Exact Tinfoil attestation GET routes don't map to models — forward
    # without model/cost/auth lookups. Do not prefix-match here: paths such as
    # /attestationjunk must continue through normal authentication.
    if request.method == "GET" and _is_tinfoil_attestation_path(path):
        if MODEL_PATH_HEADER in headers:
            return create_error_response(
                "unsupported_request",
                "Model paths do not apply to attestation",
                400,
                request=request,
            )
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
            request.state.provider = upstream.provider_type
            try:
                headers = upstream.prepare_headers(dict(request.headers))
                response = await upstream.forward_get_request(request, path, headers)
                if (
                    response.status_code in [424, 502, 503, 429]
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
            "upstream_error",
            "All upstreams failed",
            UPSTREAM_ERROR_STATUS,
            request=request,
            code=UPSTREAM_UNAVAILABLE,
            error_scope=ERROR_SCOPE_UPSTREAM,
        )

    selector: ModelPathSelector | None = None
    if MODEL_PATH_HEADER in headers:
        selector = decode_model_path(headers[MODEL_PATH_HEADER])
        if (
            selector is None
            or sum(
                name.lower() == MODEL_PATH_HEADER for name, _ in request.headers.items()
            )
            != 1
        ):
            return create_error_response(
                "invalid_request",
                f"Malformed {MODEL_PATH_HEADER} header",
                400,
                request=request,
            )
        if not isinstance(model_id, str) or not _model_ids_match(
            model_id, selector.model_id
        ):
            return create_error_response(
                "invalid_request",
                f"{MODEL_PATH_HEADER} selects model '{selector.model_id}' but the "
                f"request asks for '{model_id}'",
                400,
                request=request,
            )
        if "models" in request_body_dict:
            return create_error_response(
                "invalid_request",
                "Model paths cannot be combined with model fallbacks",
                400,
                request=request,
            )
        model_id = selector.model_id

    candidates = get_candidates(model_id)

    if not candidates:
        return create_error_response(
            "invalid_model", f"Model '{model_id}' not found", 400, request=request
        )

    if selector is not None:
        pinned = _candidate_for_selector(selector, candidates)
        if pinned is None:
            target = (
                f"provider {selector.provider_id}"
                if selector.provider_id is not None
                else f"'{selector.base_url}'"
            )
            return create_error_response(
                "invalid_model_path",
                f"Model '{selector.model_id}' is not routable through {target}",
                404,
                request=request,
            )
        # Explicit routes must never enter cross-provider failover.
        candidates = [pinned]

        if selector.endpoint_tag:
            if (
                is_ehbp
                or not request_body_dict
                or not is_openrouter_base_url(pinned[1].base_url)
                or _canonical_api_path(path)
                not in {"chat/completions", "completions", "responses"}
            ):
                return create_error_response(
                    "unsupported_request",
                    "Endpoint pinning requires an OpenRouter completion or Responses JSON request",
                    400,
                    request=request,
                )
            provider_options = request_body_dict.get("provider", {})
            if not isinstance(provider_options, dict):
                return create_error_response(
                    "invalid_request",
                    "provider must be an object",
                    400,
                    request=request,
                )
            request_body_dict = {
                **request_body_dict,
                "provider": {
                    **provider_options,
                    "order": [selector.endpoint_tag],
                    "allow_fallbacks": False,
                },
            }
            request_body = json.dumps(request_body_dict).encode()
            candidates = [
                (
                    await _price_pinned_endpoint(session, selector, *pinned),
                    pinned[1],
                )
            ]

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

    # A provider that just failed this model repeatedly is skipped while some
    # other candidate can serve it. An explicit route is never rerouted.
    if selector is None:
        healthy = [
            candidate
            for candidate in candidates
            if not is_cooling_down(
                provider_identity(candidate[1]),
                candidate_model_identity(candidate[0], model_id),
            )
        ]
        if healthy:
            candidates = healthy

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

    check_token_balance(headers, request_body_dict, max_cost_for_model)

    if x_cashu := headers.get("x-cashu", None):
        last_error = None
        for i, (model_obj, upstream) in enumerate(candidates):
            _attribute_request(request, model_obj, upstream)
            try:
                if is_ehbp:
                    if not upstream.supports_ehbp:
                        logger.warning(
                            "Upstream %s does not support EHBP for model=%s",
                            upstream.provider_type,
                            model_id,
                        )
                        continue
                    response = await forward_ehbp_x_cashu_request(
                        request=request,
                        x_cashu_token=x_cashu,
                        path=path,
                        max_cost_for_model=max_cost_for_model,
                        model_obj=model_obj,
                        upstream=upstream,
                    )
                elif is_responses_api:
                    response = await upstream.handle_x_cashu_responses(
                        request,
                        x_cashu,
                        path,
                        max_cost_for_model,
                        model_obj,
                        request_body=request_body,
                    )
                else:
                    response = await upstream.handle_x_cashu(
                        request,
                        x_cashu,
                        path,
                        max_cost_for_model,
                        model_obj,
                        request_body=request_body,
                    )
                if _upstream_response_failure(response):
                    record_failure(
                        provider_identity(upstream),
                        candidate_model_identity(model_obj, model_id),
                    )
                return response
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
                if e.scope == ERROR_SCOPE_UPSTREAM and _counts_toward_cooldown(
                    e.status_code
                ):
                    record_failure(
                        provider_identity(upstream),
                        candidate_model_identity(model_obj, model_id),
                    )
                if i == len(candidates) - 1:
                    last_error = e
                continue

        if last_error is not None:
            return create_upstream_error_response(last_error, request)
        return create_error_response(
            "upstream_error",
            "All upstreams failed",
            UPSTREAM_ERROR_STATUS,
            request=request,
            code=UPSTREAM_UNAVAILABLE,
            error_scope=ERROR_SCOPE_UPSTREAM,
        )

    elif auth := headers.get("authorization", None):
        key = await get_bearer_token_key(
            headers, path, session, auth, max_cost_for_model, model_id
        )
        mark(request, "auth")

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
        for i, (model_obj, upstream) in enumerate(candidates):
            _attribute_request(request, model_obj, upstream)
            try:
                headers = upstream.prepare_headers(dict(request.headers))
                response = await upstream.forward_get_request(request, path, headers)

                if (
                    response.status_code in [424, 502, 503, 429]
                    and i < len(candidates) - 1
                ):
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
            "upstream_error",
            "All upstreams failed",
            UPSTREAM_ERROR_STATUS,
            request=request,
            code=UPSTREAM_UNAVAILABLE,
            error_scope=ERROR_SCOPE_UPSTREAM,
        )

    reservation_snapshot: ReservationSnapshot | None = None
    if is_ehbp or request_body_dict:
        reservation_snapshot = await pay_for_request(key, max_cost_for_model, session)
        # pay_for_request refreshes the key after committing the reservation.
        # End that read transaction before waiting on upstream response headers.
        await _finish_read_transaction(session)

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
            if candidate_max > max_cost_for_model:
                await revert_pay_for_request(
                    key, session, max_cost_for_model, reservation_snapshot
                )
                try:
                    reservation_snapshot = await pay_for_request(
                        key, candidate_max, session
                    )
                except HTTPException:
                    if i == len(candidates) - 1:
                        raise
                    reservation_snapshot = await pay_for_request(
                        key, max_cost_for_model, session
                    )
                    await _finish_read_transaction(session)
                    continue
                await _finish_read_transaction(session)
                max_cost_for_model = candidate_max

        # Only once the candidate is actually tried: a fallback skipped for its
        # reservation must not take over the last attempted upstream's line.
        _attribute_request(request, model_obj, upstream)
        retries_left = settings.upstream_5xx_retry_attempts
        retry_index = 0
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
                except UpstreamError as e:
                    # Only a gateway status the upstream itself answered with:
                    # re-sending the buffered body cannot double-bill. A 502 this
                    # proxy invented for a transport error or timeout is not
                    # retried — that request may already be running upstream.
                    if (
                        e.from_upstream_response
                        and e.status_code in _RETRYABLE_UPSTREAM_5XX
                        and retries_left > 0
                    ):
                        retries_left -= 1
                        retry_index += 1
                        logger.warning(
                            "Upstream %s returned %s for model=%s; retrying same "
                            "upstream (attempt %s, %s retries left)",
                            upstream.provider_type,
                            e.status_code,
                            model_id,
                            retry_index + 1,
                            retries_left,
                            extra={
                                "provider": upstream.provider_type,
                                "model": model_id,
                                "status_code": e.status_code,
                                "path": path,
                                "retries_left": retries_left,
                            },
                        )
                        await asyncio.sleep(
                            _UPSTREAM_5XX_RETRY_BACKOFF_SECONDS * retry_index
                        )
                        continue
                    # Let the outer UpstreamError handler manage failover/revert
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

                # Same-provider recovery must not relax an explicit route.
                if response.status_code == 400 and not is_ehbp:
                    correction = correct_request(
                        request_body,
                        extract_error_message(response),
                        already_stripped,
                    )
                    if correction is not None and selector is not None:
                        corrected_body = json.loads(correction.body)
                        if any(
                            corrected_body.get(field) != request_body_dict.get(field)
                            for field in ("model", "provider")
                        ):
                            correction = None
                    if correction is not None:
                        request_body, bad_param = correction.body, correction.label
                        already_stripped.add(bad_param)
                        logger.warning(
                            "Upstream %s rejected param '%s' for model=%s; "
                            "correcting and retrying same upstream",
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
                if _upstream_response_failure(response):
                    record_failure(
                        provider_identity(upstream),
                        candidate_model_identity(model_obj, model_id),
                    )
                # 424 is an upstream failure re-reported by error_scope.
                # 502/503 are upstream errors, 429 rate limits.
                should_retry = response.status_code in [
                    424,
                    502,
                    503,
                    429,
                    400,
                    401,
                    403,
                    404,
                ]
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
            if e.scope == ERROR_SCOPE_UPSTREAM and _counts_toward_cooldown(
                e.status_code
            ):
                record_failure(
                    provider_identity(upstream),
                    candidate_model_identity(model_obj, model_id),
                )
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
        "upstream_error",
        "All upstreams failed",
        UPSTREAM_ERROR_STATUS,
        request=request,
        code=UPSTREAM_UNAVAILABLE,
        error_scope=ERROR_SCOPE_UPSTREAM,
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
    except HTTPException as error:
        detail: dict[str, Any] = error.detail if isinstance(error.detail, dict) else {}
        raw_error = detail.get("error")
        error_info = raw_error if isinstance(raw_error, dict) else {}
        logger.warning(
            "Bearer token rejected",
            extra={
                "status_code": error.status_code,
                "error_code": error_info.get("code"),
                "path": path,
                "model_id": model_id,
                "required_msat": min_cost,
            },
        )
        raise
    except Exception as error:
        logger.exception(
            "Bearer token validation failed",
            extra={
                "error_type": type(error).__name__,
                "path": path,
                "model_id": model_id,
                "required_msat": min_cost,
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
