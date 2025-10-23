from __future__ import annotations

import json
import os
import re
import traceback
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Mapping

import httpx

if TYPE_CHECKING:
    from .core.settings import Settings
    from .payment.cost_caculation import CostData, MaxCostData

from fastapi import BackgroundTasks, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from .auth import adjust_payment_for_tokens
from .core import get_logger
from .core.db import ApiKey, AsyncSession, ModelRow, UpstreamProviderRow, create_session
from .payment.helpers import create_error_response
from .payment.models import Model, async_fetch_openrouter_models

logger = get_logger(__name__)


def resolve_model_alias(model_id: str, canonical_slug: str | None = None) -> list[str]:
    """Resolve model ID to all possible aliases.

    Returns list of aliases including canonical slug and variations without provider prefix.

    Args:
        model_id: Model identifier (e.g., "gpt-5-mini" or "openai/gpt-5-mini")
        canonical_slug: Optional canonical slug from provider (e.g., "openai/gpt-5-pro-2025-10-06")

    Returns:
        List of possible model ID aliases
    """
    aliases = [model_id]

    base_model = model_id
    if "/" in model_id:
        without_prefix = model_id.split("/", 1)[1]
        aliases.append(without_prefix)
        base_model = without_prefix

    date_pattern = re.compile(r"-\d{4}-\d{2}-\d{2}$")
    if date_pattern.search(base_model):
        base_without_date = date_pattern.sub("", base_model)
        if base_without_date not in aliases:
            aliases.append(base_without_date)
        if "/" in model_id:
            prefix = model_id.split("/", 1)[0]
            prefixed_without_date = f"{prefix}/{base_without_date}"
            if prefixed_without_date not in aliases:
                aliases.append(prefixed_without_date)

    if canonical_slug and canonical_slug not in aliases:
        aliases.append(canonical_slug)
        if "/" in canonical_slug:
            canonical_without_prefix = canonical_slug.split("/", 1)[1]
            if canonical_without_prefix not in aliases:
                aliases.append(canonical_without_prefix)
            if date_pattern.search(canonical_without_prefix):
                canonical_base = date_pattern.sub("", canonical_without_prefix)
                if canonical_base not in aliases:
                    aliases.append(canonical_base)

    return aliases


async def get_all_models_with_overrides(
    upstreams: list[UpstreamProvider],
) -> list[Model]:
    """Get all models from all providers with database overrides applied.

    Models in the database with upstream_provider_id set are treated as overrides
    that replace the provider's model with the same ID.

    Args:
        upstreams: List of upstream provider instances

    Returns:
        List of Model objects with overrides applied
    """
    from sqlmodel import select

    from .payment.models import _row_to_model

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

    all_models: dict[str, Model] = {}

    for upstream in upstreams:
        for model in upstream.get_cached_models():
            if model.id in overrides_by_id:
                override_row, provider_fee = overrides_by_id[model.id]
                all_models[model.id] = _row_to_model(
                    override_row, apply_provider_fee=True, provider_fee=provider_fee
                )
            elif model.enabled:
                all_models[model.id] = model

    return list(all_models.values())


async def get_model_with_override(
    model_id: str,
    upstreams: list[UpstreamProvider],
    session: AsyncSession,
) -> Model | None:
    """Get a specific model from providers with database override applied.

    Resolves model aliases automatically (e.g., both "gpt-5-mini" and "openai/gpt-5-mini").

    Args:
        model_id: Model identifier (with or without provider prefix)
        upstreams: List of upstream provider instances

    Returns:
        Model object or None if not found
    """
    from sqlmodel import select

    from .payment.models import _row_to_model

    aliases = resolve_model_alias(model_id)

    for alias in aliases:
        result = await session.exec(
            select(ModelRow).where(
                ModelRow.id == alias,
                ModelRow.upstream_provider_id.isnot(None),  # type: ignore
                ModelRow.enabled,
            )
        )
        override_row = result.first()
        if override_row:
            provider = await session.get(
                UpstreamProviderRow, override_row.upstream_provider_id
            )
            provider_fee = provider.provider_fee if provider else 1.01
            return _row_to_model(
                override_row, apply_provider_fee=True, provider_fee=provider_fee
            )

    for alias in aliases:
        for upstream in upstreams:
            model = upstream.get_cached_model_by_id(alias)
            if model and model.enabled:
                return model

    return None


async def refresh_upstreams_models_periodically(
    upstreams: list[UpstreamProvider],
) -> None:
    """Background task to periodically refresh models cache for all providers.

    Args:
        upstreams: List of upstream provider instances
    """
    import asyncio
    import random

    from .core.settings import settings

    interval = getattr(settings, "models_refresh_interval_seconds", 0)
    if not interval or interval <= 0:
        logger.info("Provider models refresh disabled (interval <= 0)")
        return

    while True:
        try:
            for upstream in upstreams:
                try:
                    await upstream.refresh_models_cache()
                except Exception as e:
                    logger.error(
                        f"Error refreshing models for {upstream.upstream_name or upstream.base_url}",
                        extra={"error": str(e), "error_type": type(e).__name__},
                    )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(
                "Error in provider models refresh loop",
                extra={"error": str(e), "error_type": type(e).__name__},
            )

        try:
            jitter = max(0.0, float(interval) * 0.1)
            await asyncio.sleep(interval + random.uniform(0, jitter))
        except asyncio.CancelledError:
            break


async def init_upstreams() -> list[UpstreamProvider]:
    """Initialize upstream providers from database.

    Seeds database with providers from settings if empty, then loads and instantiates
    provider instances from database records, and refreshes their models cache.
    """
    from sqlmodel import select

    from .core.settings import settings

    async with create_session() as session:
        result = await session.exec(select(UpstreamProviderRow))
        existing_providers = result.all()

        if not existing_providers:
            logger.info(
                "No upstream providers found in database, seeding from settings"
            )
            await _seed_providers_from_settings(session, settings)
            await session.commit()
            result = await session.exec(select(UpstreamProviderRow))
            existing_providers = result.all()

        upstreams: list[UpstreamProvider] = []
        for provider_row in existing_providers:
            if not provider_row.enabled:
                logger.debug(f"Skipping disabled provider: {provider_row.base_url}")
                continue

            provider = _instantiate_provider(provider_row)
            if provider:
                await provider.refresh_models_cache()
                upstreams.append(provider)
                logger.info(
                    f"Initialized {provider_row.provider_type} provider",
                    extra={
                        "base_url": provider_row.base_url,
                        "models_cached": len(provider.get_cached_models()),
                    },
                )

        return upstreams


async def _seed_providers_from_settings(
    session: AsyncSession, settings: "Settings"
) -> None:
    """Seed database with upstream providers from environment variables.

    Args:
        session: Database session
    """
    from sqlmodel import select

    from .core.settings import settings

    providers_to_add: list[UpstreamProviderRow] = []
    seeded_base_urls: set[str] = set()

    openai_api_key = os.environ.get("OPENAI_API_KEY")
    if openai_api_key:
        base_url = "https://api.openai.com/v1"
        result = await session.exec(
            select(UpstreamProviderRow).where(UpstreamProviderRow.base_url == base_url)
        )
        if not result.first():
            providers_to_add.append(
                UpstreamProviderRow(
                    provider_type="openai",
                    base_url=base_url,
                    api_key=openai_api_key,
                    enabled=True,
                )
            )
            seeded_base_urls.add(base_url)

    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_api_key:
        base_url = "https://api.anthropic.com/v1"
        result = await session.exec(
            select(UpstreamProviderRow).where(UpstreamProviderRow.base_url == base_url)
        )
        if not result.first():
            providers_to_add.append(
                UpstreamProviderRow(
                    provider_type="anthropic",
                    base_url=base_url,
                    api_key=anthropic_api_key,
                    enabled=True,
                )
            )
            seeded_base_urls.add(base_url)

    openrouter_api_key = os.environ.get("OPENROUTER_API_KEY")
    if openrouter_api_key:
        base_url = "https://openrouter.ai/api/v1"
        result = await session.exec(
            select(UpstreamProviderRow).where(UpstreamProviderRow.base_url == base_url)
        )
        if not result.first():
            providers_to_add.append(
                UpstreamProviderRow(
                    provider_type="openrouter",
                    base_url=base_url,
                    api_key=openrouter_api_key,
                    enabled=True,
                )
            )
            seeded_base_urls.add(base_url)

    if settings.chat_completions_api_version and settings.upstream_base_url:
        base_url = settings.upstream_base_url
        if base_url not in seeded_base_urls:
            result = await session.exec(
                select(UpstreamProviderRow).where(
                    UpstreamProviderRow.base_url == base_url
                )
            )
            if not result.first():
                providers_to_add.append(
                    UpstreamProviderRow(
                        provider_type="azure",
                        base_url=base_url,
                        api_key=settings.upstream_api_key,
                        api_version=settings.chat_completions_api_version,
                        enabled=True,
                    )
                )
                seeded_base_urls.add(base_url)

    if settings.upstream_base_url and settings.upstream_api_key:
        base_url = settings.upstream_base_url
        if base_url not in seeded_base_urls:
            result = await session.exec(
                select(UpstreamProviderRow).where(
                    UpstreamProviderRow.base_url == base_url
                )
            )
            if not result.first():
                if "api.openai.com" in base_url.lower():
                    providers_to_add.append(
                        UpstreamProviderRow(
                            provider_type="openai",
                            base_url=base_url,
                            api_key=settings.upstream_api_key,
                            enabled=True,
                        )
                    )
                elif "api.anthropic.com" in base_url.lower():
                    providers_to_add.append(
                        UpstreamProviderRow(
                            provider_type="anthropic",
                            base_url=base_url,
                            api_key=settings.upstream_api_key,
                            enabled=True,
                        )
                    )
                elif "openrouter.ai/api/v1" in base_url.lower():
                    providers_to_add.append(
                        UpstreamProviderRow(
                            provider_type="openrouter",
                            base_url=base_url,
                            api_key=settings.upstream_api_key,
                            enabled=True,
                        )
                    )
                else:
                    providers_to_add.append(
                        UpstreamProviderRow(
                            provider_type="custom",
                            base_url=base_url,
                            api_key=settings.upstream_api_key,
                            enabled=True,
                        )
                    )
                seeded_base_urls.add(base_url)

    for provider in providers_to_add:
        session.add(provider)
        logger.info(
            f"Seeding {provider.provider_type} provider",
            extra={"base_url": provider.base_url},
        )


def _instantiate_provider(provider_row: UpstreamProviderRow) -> UpstreamProvider | None:
    """Instantiate an UpstreamProvider from a database row.

    Args:
        provider_row: Database row containing provider configuration

    Returns:
        Instantiated provider or None if provider type is unknown
    """
    try:
        if provider_row.provider_type == "openai":
            return OpenAIUpstreamProvider(
                provider_row.api_key, provider_row.provider_fee
            )
        elif provider_row.provider_type == "anthropic":
            return AnthropicUpstreamProvider(
                provider_row.api_key, provider_row.provider_fee
            )
        elif provider_row.provider_type == "azure":
            if not provider_row.api_version:
                logger.error(
                    "Azure provider missing api_version",
                    extra={"base_url": provider_row.base_url},
                )
                return None
            return AzureUpstreamProvider(
                provider_row.base_url,
                provider_row.api_key,
                provider_row.api_version,
                provider_row.provider_fee,
            )
        elif provider_row.provider_type == "openrouter":
            return OpenRouterUpstreamProvider(
                provider_row.api_key, provider_row.provider_fee
            )
        elif provider_row.provider_type == "custom":
            return UpstreamProvider(
                provider_row.base_url, provider_row.api_key, provider_row.provider_fee
            )
        else:
            logger.error(
                f"Unknown provider type: {provider_row.provider_type}",
                extra={"base_url": provider_row.base_url},
            )
            return None
    except Exception as e:
        logger.error(
            f"Failed to instantiate provider: {e}",
            extra={
                "provider_type": provider_row.provider_type,
                "base_url": provider_row.base_url,
                "error": str(e),
            },
        )
        return None


class UpstreamProvider:
    """Provider for forwarding requests to an upstream AI service API."""

    base_url: str
    api_key: str
    upstream_name: str | None = None
    provider_fee: float = 1.05
    _models_cache: list[Model] = []
    _models_by_id: dict[str, Model] = {}

    def __init__(self, base_url: str, api_key: str, provider_fee: float = 1.01):
        """Initialize the upstream provider.

        Args:
            base_url: Base URL of the upstream API endpoint
            api_key: API key for authenticating with the upstream service
            provider_fee: Provider fee multiplier (default 1.01 for 1% fee)
        """
        self.base_url = base_url
        self.api_key = api_key
        self.provider_fee = provider_fee
        self._models_cache = []
        self._models_by_id = {}

    def prepare_headers(self, request_headers: dict) -> dict:
        """Prepare headers for upstream request by removing proxy-specific headers and adding authentication.

        Args:
            request_headers: Original request headers from the client

        Returns:
            Headers dict ready for upstream forwarding with authentication added
        """
        logger.debug(
            "Preparing upstream headers",
            extra={
                "original_headers_count": len(request_headers),
                "has_upstream_api_key": bool(self.api_key),
            },
        )

        headers = dict(request_headers)
        removed_headers = []

        for header in [
            "host",
            "content-length",
            "refund-lnurl",
            "key-expiry-time",
            "x-cashu",
        ]:
            if headers.pop(header, None) is not None:
                removed_headers.append(header)

        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            if headers.pop("authorization", None) is not None:
                removed_headers.append("authorization (replaced with upstream key)")
        else:
            for auth_header in ["Authorization", "authorization"]:
                if headers.pop(auth_header, None) is not None:
                    removed_headers.append(auth_header)

        logger.debug(
            "Headers prepared for upstream",
            extra={
                "final_headers_count": len(headers),
                "removed_headers": removed_headers,
                "added_upstream_auth": bool(self.api_key),
            },
        )

        return headers

    def prepare_params(
        self, path: str, query_params: Mapping[str, str] | None
    ) -> Mapping[str, str]:
        """Prepare query parameters for upstream request.

        Base implementation passes through query params unchanged. Override in subclasses for provider-specific params.

        Args:
            path: Request path
            query_params: Original query parameters from the client

        Returns:
            Query parameters dict ready for upstream forwarding
        """
        return query_params or {}

    def transform_model_name(self, model_id: str) -> str:
        """Transform model ID for this provider's API format.

        Base implementation returns model_id unchanged. Override in subclasses for provider-specific transformations.

        Args:
            model_id: Model identifier (may include provider prefix)

        Returns:
            Transformed model ID for this provider
        """
        return model_id

    def prepare_request_body(self, body: bytes | None) -> bytes | None:
        """Transform request body for provider-specific requirements.

        Automatically transforms model names in the request body.

        Args:
            body: Original request body bytes

        Returns:
            Transformed request body bytes
        """
        if not body:
            return body

        try:
            data = json.loads(body)
            if isinstance(data, dict) and "model" in data:
                original_model = data["model"]
                transformed_model = self.transform_model_name(original_model)
                if transformed_model != original_model:
                    data["model"] = transformed_model
                    logger.debug(
                        "Transformed model name in request",
                        extra={
                            "original": original_model,
                            "transformed": transformed_model,
                            "provider": self.upstream_name or self.base_url,
                        },
                    )
                    return json.dumps(data).encode()
        except Exception as e:
            logger.debug(
                "Could not transform request body",
                extra={
                    "error": str(e),
                    "provider": self.upstream_name or self.base_url,
                },
            )

        return body

    def _extract_upstream_error_message(
        self, body_bytes: bytes
    ) -> tuple[str, str | None]:
        """Extract error message and code from upstream error response body.

        Args:
            body_bytes: Raw response body bytes from upstream

        Returns:
            Tuple of (error_message, error_code), where error_code may be None
        """
        message: str = "Upstream request failed"
        upstream_code: str | None = None
        if not body_bytes:
            return message, upstream_code
        try:
            data = json.loads(body_bytes)
            if isinstance(data, dict):
                err = data.get("error")
                if isinstance(err, dict):
                    raw_msg = (
                        err.get("message") or err.get("detail") or err.get("error")
                    )
                    if isinstance(raw_msg, (str, int, float)):
                        message = str(raw_msg)
                    upstream_code_raw = err.get("code") or err.get("type")
                    if isinstance(upstream_code_raw, (str, int, float)):
                        upstream_code = str(upstream_code_raw)
                elif "message" in data and isinstance(
                    data["message"], (str, int, float)
                ):
                    message = str(data["message"])  # type: ignore[arg-type]
                elif "detail" in data and isinstance(data["detail"], (str, int, float)):
                    message = str(data["detail"])  # type: ignore[arg-type]
        except Exception:
            preview = body_bytes.decode("utf-8", errors="ignore").strip()
            if preview:
                message = preview[:500]
        return message, upstream_code

    async def map_upstream_error_response(
        self, request: Request, path: str, upstream_response: httpx.Response
    ) -> Response:
        """Map upstream error responses to appropriate proxy error responses.

        Args:
            request: Original FastAPI request
            path: Request path
            upstream_response: Response from upstream service

        Returns:
            Mapped error response with appropriate status code and error type
        """
        status_code = upstream_response.status_code
        headers = dict(upstream_response.headers)
        content_type = headers.get("content-type", "")
        try:
            body_bytes = await upstream_response.aread()
        except Exception:
            body_bytes = b""

        message, upstream_code = self._extract_upstream_error_message(body_bytes)
        lowered_message = message.lower()
        lowered_code = (upstream_code or "").lower()

        error_type = "upstream_error"
        mapped_status = 502

        if status_code in (400, 422):
            error_type = "invalid_request_error"
            mapped_status = 400
        elif status_code in (401, 403):
            error_type = "upstream_auth_error"
            mapped_status = 502
        elif status_code == 404:
            if path.endswith("chat/completions"):
                error_type = "invalid_model"
                mapped_status = 400
                if not message or message == "Upstream request failed":
                    message = "Requested model is not available upstream"
            elif "model" in lowered_message or "model" in lowered_code:
                error_type = "invalid_model"
                mapped_status = 400
                if not message or message == "Upstream request failed":
                    message = "Requested model is not available upstream"
            else:
                error_type = "upstream_error"
                mapped_status = 502
        elif status_code == 429:
            error_type = "rate_limit_exceeded"
            mapped_status = 429
        elif status_code >= 500:
            error_type = "upstream_error"
            mapped_status = 502

        logger.debug(
            "Mapped upstream error",
            extra={
                "path": path,
                "upstream_status": status_code,
                "mapped_status": mapped_status,
                "error_type": error_type,
                "upstream_content_type": content_type,
                "message_preview": message[:200],
            },
        )

        return create_error_response(
            error_type, message, mapped_status, request=request
        )

    async def handle_streaming_chat_completion(
        self, response: httpx.Response, key: ApiKey, max_cost_for_model: int
    ) -> StreamingResponse:
        """Handle streaming chat completion responses with token usage tracking and cost adjustment.

        Args:
            response: Streaming response from upstream
            key: API key for the authenticated user
            max_cost_for_model: Maximum cost deducted upfront for the model

        Returns:
            StreamingResponse with cost data injected at the end
        """
        logger.info(
            "Processing streaming chat completion",
            extra={
                "key_hash": key.hashed_key[:8] + "...",
                "key_balance": key.balance,
                "response_status": response.status_code,
            },
        )

        async def stream_with_cost(
            max_cost_for_model: int,
        ) -> AsyncGenerator[bytes, None]:
            stored_chunks: list[bytes] = []
            usage_finalized: bool = False
            last_model_seen: str | None = None

            async def finalize_without_usage() -> bytes | None:
                nonlocal usage_finalized
                if usage_finalized:
                    return None
                async with create_session() as new_session:
                    fresh_key = await new_session.get(key.__class__, key.hashed_key)
                    if not fresh_key:
                        return None
                    try:
                        fallback: dict = {
                            "model": last_model_seen or "unknown",
                            "usage": None,
                        }
                        cost_data = await adjust_payment_for_tokens(
                            fresh_key, fallback, new_session, max_cost_for_model
                        )
                        usage_finalized = True
                        logger.info(
                            "Finalized streaming payment without explicit usage",
                            extra={
                                "key_hash": key.hashed_key[:8] + "...",
                                "cost_data": cost_data,
                                "balance_after_adjustment": fresh_key.balance,
                            },
                        )
                        return f"data: {json.dumps({'cost': cost_data})}\n\n".encode()
                    except Exception as cost_error:
                        logger.error(
                            "Error finalizing payment without usage",
                            extra={
                                "error": str(cost_error),
                                "error_type": type(cost_error).__name__,
                                "key_hash": key.hashed_key[:8] + "...",
                            },
                        )
                        return None

            try:
                async for chunk in response.aiter_bytes():
                    stored_chunks.append(chunk)
                    try:
                        for part in re.split(b"data: ", chunk):
                            if not part or part.strip() in (b"[DONE]", b""):
                                continue
                            try:
                                obj = json.loads(part)
                                if isinstance(obj, dict) and obj.get("model"):
                                    last_model_seen = str(obj.get("model"))
                            except json.JSONDecodeError:
                                pass
                    except Exception:
                        pass

                    yield chunk

                logger.debug(
                    "Streaming completed, analyzing usage data",
                    extra={
                        "key_hash": key.hashed_key[:8] + "...",
                        "chunks_count": len(stored_chunks),
                    },
                )

                for i in range(len(stored_chunks) - 1, -1, -1):
                    chunk = stored_chunks[i]
                    if not chunk:
                        continue
                    try:
                        events = re.split(b"data: ", chunk)
                        for event_data in events:
                            if not event_data or event_data.strip() in (b"[DONE]", b""):
                                continue
                            try:
                                data = json.loads(event_data)
                                if isinstance(data, dict) and data.get("model"):
                                    last_model_seen = str(data.get("model"))
                                if isinstance(data, dict) and isinstance(
                                    data.get("usage"), dict
                                ):
                                    async with create_session() as new_session:
                                        fresh_key = await new_session.get(
                                            key.__class__, key.hashed_key
                                        )
                                        if fresh_key:
                                            try:
                                                cost_data = (
                                                    await adjust_payment_for_tokens(
                                                        fresh_key,
                                                        data,
                                                        new_session,
                                                        max_cost_for_model,
                                                    )
                                                )
                                                usage_finalized = True
                                                logger.info(
                                                    "Token adjustment completed for streaming",
                                                    extra={
                                                        "key_hash": key.hashed_key[:8]
                                                        + "...",
                                                        "cost_data": cost_data,
                                                        "balance_after_adjustment": fresh_key.balance,
                                                    },
                                                )
                                                yield f"data: {json.dumps({'cost': cost_data})}\n\n".encode()
                                            except Exception as cost_error:
                                                logger.error(
                                                    "Error adjusting payment for streaming tokens",
                                                    extra={
                                                        "error": str(cost_error),
                                                        "error_type": type(
                                                            cost_error
                                                        ).__name__,
                                                        "key_hash": key.hashed_key[:8]
                                                        + "...",
                                                    },
                                                )
                                    break
                            except json.JSONDecodeError:
                                continue
                    except Exception as e:
                        logger.error(
                            "Error processing streaming response chunk",
                            extra={
                                "error": str(e),
                                "error_type": type(e).__name__,
                                "key_hash": key.hashed_key[:8] + "...",
                            },
                        )

                if not usage_finalized:
                    maybe_cost_event = await finalize_without_usage()
                    if maybe_cost_event is not None:
                        yield maybe_cost_event

            except Exception as stream_error:
                logger.warning(
                    "Streaming interrupted; finalizing without usage",
                    extra={
                        "error": str(stream_error),
                        "error_type": type(stream_error).__name__,
                        "key_hash": key.hashed_key[:8] + "...",
                    },
                )
                await finalize_without_usage()
                raise

        return StreamingResponse(
            stream_with_cost(max_cost_for_model),
            status_code=response.status_code,
            headers=dict(response.headers),
        )

    async def handle_non_streaming_chat_completion(
        self,
        response: httpx.Response,
        key: ApiKey,
        session: AsyncSession,
        deducted_max_cost: int,
    ) -> Response:
        """Handle non-streaming chat completion responses with token usage tracking and cost adjustment.

        Args:
            response: Response from upstream
            key: API key for the authenticated user
            session: Database session for updating balance
            deducted_max_cost: Maximum cost deducted upfront

        Returns:
            Response with cost data added to JSON body
        """
        logger.info(
            "Processing non-streaming chat completion",
            extra={
                "key_hash": key.hashed_key[:8] + "...",
                "key_balance": key.balance,
                "response_status": response.status_code,
            },
        )

        try:
            content = await response.aread()
            response_json = json.loads(content)

            logger.debug(
                "Parsed response JSON",
                extra={
                    "key_hash": key.hashed_key[:8] + "...",
                    "model": response_json.get("model", "unknown"),
                    "has_usage": "usage" in response_json,
                },
            )

            cost_data = await adjust_payment_for_tokens(
                key, response_json, session, deducted_max_cost
            )
            response_json["cost"] = cost_data

            logger.info(
                "Token adjustment completed for non-streaming",
                extra={
                    "key_hash": key.hashed_key[:8] + "...",
                    "cost_data": cost_data,
                    "model": response_json.get("model", "unknown"),
                    "balance_after_adjustment": key.balance,
                },
            )

            allowed_headers = {
                "content-type",
                "cache-control",
                "date",
                "vary",
                "access-control-allow-origin",
                "access-control-allow-methods",
                "access-control-allow-headers",
                "access-control-allow-credentials",
                "access-control-expose-headers",
                "access-control-max-age",
            }

            response_headers = {
                k: v
                for k, v in response.headers.items()
                if k.lower() in allowed_headers
            }

            return Response(
                content=json.dumps(response_json).encode(),
                status_code=response.status_code,
                headers=response_headers,
                media_type="application/json",
            )
        except json.JSONDecodeError as e:
            logger.error(
                "Failed to parse JSON from upstream response",
                extra={
                    "error": str(e),
                    "key_hash": key.hashed_key[:8] + "...",
                    "content_preview": content[:200].decode(errors="ignore")
                    if content
                    else "empty",
                },
            )
            raise
        except Exception as e:
            logger.error(
                "Error processing non-streaming chat completion",
                extra={
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "key_hash": key.hashed_key[:8] + "...",
                },
            )
            raise

    async def forward_request(
        self,
        request: Request,
        path: str,
        headers: dict,
        request_body: bytes | None,
        key: ApiKey,
        max_cost_for_model: int,
        session: AsyncSession,
    ) -> Response | StreamingResponse:
        """Forward authenticated request to upstream service with cost tracking.

        Args:
            request: Original FastAPI request
            path: Request path
            headers: Prepared headers for upstream
            request_body: Request body bytes, if any
            key: API key for authenticated user
            max_cost_for_model: Maximum cost deducted upfront
            session: Database session for balance updates

        Returns:
            Response or StreamingResponse from upstream with cost tracking
        """
        if path.startswith("v1/"):
            path = path.replace("v1/", "")

        url = f"{self.base_url}/{path}"

        transformed_body = self.prepare_request_body(request_body)

        logger.info(
            "Forwarding request to upstream",
            extra={
                "url": url,
                "method": request.method,
                "path": path,
                "key_hash": key.hashed_key[:8] + "...",
                "key_balance": key.balance,
                "has_request_body": request_body is not None,
            },
        )

        client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(retries=1),
            timeout=None,
        )

        try:
            if transformed_body is not None:
                response = await client.send(
                    client.build_request(
                        request.method,
                        url,
                        headers=headers,
                        content=transformed_body,
                        params=self.prepare_params(path, request.query_params),
                    ),
                    stream=True,
                )
            else:
                response = await client.send(
                    client.build_request(
                        request.method,
                        url,
                        headers=headers,
                        content=request.stream(),
                        params=self.prepare_params(path, request.query_params),
                    ),
                    stream=True,
                )

            logger.info(
                "Received upstream response",
                extra={
                    "status_code": response.status_code,
                    "path": path,
                    "key_hash": key.hashed_key[:8] + "...",
                    "content_type": response.headers.get("content-type", "unknown"),
                },
            )

            if response.status_code != 200:
                try:
                    mapped_error = await self.map_upstream_error_response(
                        request, path, response
                    )
                finally:
                    await response.aclose()
                    await client.aclose()
                return mapped_error

            if path.endswith("chat/completions"):
                client_wants_streaming = False
                if request_body:
                    try:
                        request_data = json.loads(request_body)
                        client_wants_streaming = request_data.get("stream", False)
                        logger.debug(
                            "Chat completion request analysis",
                            extra={
                                "client_wants_streaming": client_wants_streaming,
                                "model": request_data.get("model", "unknown"),
                                "key_hash": key.hashed_key[:8] + "...",
                            },
                        )
                    except json.JSONDecodeError:
                        logger.warning(
                            "Failed to parse request body JSON for streaming detection"
                        )

                content_type = response.headers.get("content-type", "")
                upstream_is_streaming = "text/event-stream" in content_type
                is_streaming = client_wants_streaming and upstream_is_streaming

                logger.debug(
                    "Response type analysis",
                    extra={
                        "is_streaming": is_streaming,
                        "client_wants_streaming": client_wants_streaming,
                        "upstream_is_streaming": upstream_is_streaming,
                        "content_type": content_type,
                        "key_hash": key.hashed_key[:8] + "...",
                    },
                )

                if is_streaming and response.status_code == 200:
                    result = await self.handle_streaming_chat_completion(
                        response, key, max_cost_for_model
                    )
                    background_tasks = BackgroundTasks()
                    background_tasks.add_task(response.aclose)
                    background_tasks.add_task(client.aclose)
                    result.background = background_tasks
                    return result

                elif response.status_code == 200:
                    try:
                        return await self.handle_non_streaming_chat_completion(
                            response, key, session, max_cost_for_model
                        )
                    finally:
                        await response.aclose()
                        await client.aclose()

            background_tasks = BackgroundTasks()
            background_tasks.add_task(response.aclose)
            background_tasks.add_task(client.aclose)

            logger.debug(
                "Streaming non-chat response",
                extra={
                    "path": path,
                    "status_code": response.status_code,
                    "key_hash": key.hashed_key[:8] + "...",
                },
            )

            return StreamingResponse(
                response.aiter_bytes(),
                status_code=response.status_code,
                headers=dict(response.headers),
                background=background_tasks,
            )

        except httpx.RequestError as exc:
            await client.aclose()
            error_type = type(exc).__name__
            error_details = str(exc)

            logger.error(
                "HTTP request error to upstream",
                extra={
                    "error_type": error_type,
                    "error_details": error_details,
                    "method": request.method,
                    "url": url,
                    "path": path,
                    "query_params": dict(request.query_params),
                    "key_hash": key.hashed_key[:8] + "...",
                },
            )

            if isinstance(exc, httpx.ConnectError):
                error_message = "Unable to connect to upstream service"
            elif isinstance(exc, httpx.TimeoutException):
                error_message = "Upstream service request timed out"
            elif isinstance(exc, httpx.NetworkError):
                error_message = "Network error while connecting to upstream service"
            else:
                error_message = f"Error connecting to upstream service: {error_type}"

            return create_error_response(
                "upstream_error", error_message, 502, request=request
            )

        except Exception as exc:
            await client.aclose()
            tb = traceback.format_exc()

            logger.error(
                "Unexpected error in upstream forwarding",
                extra={
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "method": request.method,
                    "url": url,
                    "path": path,
                    "query_params": dict(request.query_params),
                    "key_hash": key.hashed_key[:8] + "...",
                    "traceback": tb,
                },
            )

            return create_error_response(
                "internal_error",
                "An unexpected server error occurred",
                500,
                request=request,
            )

    async def forward_get_request(
        self,
        request: Request,
        path: str,
        headers: dict,
    ) -> Response | StreamingResponse:
        """Forward unauthenticated GET request to upstream service.

        Args:
            request: Original FastAPI request
            path: Request path
            headers: Prepared headers for upstream

        Returns:
            StreamingResponse from upstream
        """
        if path.startswith("v1/"):
            path = path.replace("v1/", "")

        url = f"{self.base_url}/{path}"

        logger.info(
            "Forwarding GET request to upstream",
            extra={"url": url, "method": request.method, "path": path},
        )

        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(retries=1),
            timeout=None,
        ) as client:
            try:
                response = await client.send(
                    client.build_request(
                        request.method,
                        url,
                        headers=headers,
                        content=request.stream(),
                        params=self.prepare_params(path, request.query_params),
                    ),
                )

                logger.info(
                    "GET request forwarded successfully",
                    extra={"path": path, "status_code": response.status_code},
                )
                if response.status_code != 200:
                    try:
                        mapped = await self.map_upstream_error_response(
                            request, path, response
                        )
                    finally:
                        await response.aclose()
                    return mapped

                return StreamingResponse(
                    response.aiter_bytes(),
                    status_code=response.status_code,
                    headers=dict(response.headers),
                )
            except Exception as exc:
                tb = traceback.format_exc()
                logger.error(
                    "Error forwarding GET request",
                    extra={
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                        "method": request.method,
                        "url": url,
                        "path": path,
                        "query_params": dict(request.query_params),
                        "traceback": tb,
                    },
                )
                return create_error_response(
                    "internal_error",
                    "An unexpected server error occurred",
                    500,
                    request=request,
                )

    async def get_x_cashu_cost(
        self, response_data: dict, max_cost_for_model: int
    ) -> MaxCostData | CostData | None:
        """Calculate cost for X-Cashu payment based on response data.

        Args:
            response_data: Response data containing model and usage information
            max_cost_for_model: Maximum cost for the model

        Returns:
            Cost data object (MaxCostData or CostData) or None if calculation fails
        """
        from .payment.cost_caculation import (
            CostData,
            CostDataError,
            MaxCostData,
            calculate_cost,
        )

        model = response_data.get("model", None)
        logger.debug(
            "Calculating cost for response",
            extra={"model": model, "has_usage": "usage" in response_data},
        )

        async with create_session() as session:
            match await calculate_cost(response_data, max_cost_for_model, session):
                case MaxCostData() as cost:
                    logger.debug(
                        "Using max cost pricing",
                        extra={"model": model, "max_cost_msats": cost.total_msats},
                    )
                    return cost
                case CostData() as cost:
                    logger.debug(
                        "Using token-based pricing",
                        extra={
                            "model": model,
                            "total_cost_msats": cost.total_msats,
                            "input_msats": cost.input_msats,
                            "output_msats": cost.output_msats,
                        },
                    )
                    return cost
                case CostDataError() as error:
                    logger.error(
                        "Cost calculation error",
                        extra={
                            "model": model,
                            "error_message": error.message,
                            "error_code": error.code,
                        },
                    )
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "error": {
                                "message": error.message,
                                "type": "invalid_request_error",
                                "code": error.code,
                            }
                        },
                    )
        return None

    async def send_refund(self, amount: int, unit: str, mint: str | None = None) -> str:
        """Create and send a refund token to the user.

        Args:
            amount: Refund amount
            unit: Unit of the refund (sat or msat)
            mint: Optional mint URL for the refund token

        Returns:
            Refund token string
        """
        from .wallet import send_token

        logger.debug(
            "Creating refund token",
            extra={"amount": amount, "unit": unit, "mint": mint},
        )

        max_retries = 3
        last_exception = None

        for attempt in range(max_retries):
            try:
                refund_token = await send_token(amount, unit=unit, mint_url=mint)

                logger.info(
                    "Refund token created successfully",
                    extra={
                        "amount": amount,
                        "unit": unit,
                        "mint": mint,
                        "attempt": attempt + 1,
                        "token_preview": refund_token[:20] + "..."
                        if len(refund_token) > 20
                        else refund_token,
                    },
                )

                return refund_token
            except Exception as e:
                last_exception = e
                if attempt < max_retries - 1:
                    logger.warning(
                        "Refund token creation failed, retrying",
                        extra={
                            "error": str(e),
                            "error_type": type(e).__name__,
                            "attempt": attempt + 1,
                            "max_retries": max_retries,
                            "amount": amount,
                            "unit": unit,
                            "mint": mint,
                        },
                    )
                else:
                    logger.error(
                        "Failed to create refund token after all retries",
                        extra={
                            "error": str(e),
                            "error_type": type(e).__name__,
                            "attempt": attempt + 1,
                            "max_retries": max_retries,
                            "amount": amount,
                            "unit": unit,
                            "mint": mint,
                        },
                    )

        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": f"failed to create refund after {max_retries} attempts: {str(last_exception)}",
                    "type": "invalid_request_error",
                    "code": "send_token_failed",
                }
            },
        )

    async def handle_x_cashu_streaming_response(
        self,
        content_str: str,
        response: httpx.Response,
        amount: int,
        unit: str,
        max_cost_for_model: int,
    ) -> StreamingResponse:
        """Handle streaming response for X-Cashu payment, calculating refund if needed.

        Args:
            content_str: Response content as string
            response: Original httpx response
            amount: Payment amount received
            unit: Payment unit (sat or msat)
            max_cost_for_model: Maximum cost for the model

        Returns:
            StreamingResponse with refund token in header if applicable
        """
        logger.debug(
            "Processing streaming response",
            extra={
                "amount": amount,
                "unit": unit,
                "content_lines": len(content_str.strip().split("\n")),
            },
        )

        response_headers = dict(response.headers)
        if "transfer-encoding" in response_headers:
            del response_headers["transfer-encoding"]
        if "content-encoding" in response_headers:
            del response_headers["content-encoding"]

        usage_data = None
        model = None

        lines = content_str.strip().split("\n")
        for line in lines:
            if line.startswith("data: "):
                try:
                    data_json = json.loads(line[6:])
                    if "usage" in data_json:
                        usage_data = data_json["usage"]
                        model = data_json.get("model")
                    elif "model" in data_json and not model:
                        model = data_json["model"]
                except json.JSONDecodeError:
                    continue

        if usage_data and model:
            logger.debug(
                "Found usage data in streaming response",
                extra={
                    "model": model,
                    "usage_data": usage_data,
                    "amount": amount,
                    "unit": unit,
                },
            )

            response_data = {"usage": usage_data, "model": model}
            try:
                cost_data = await self.get_x_cashu_cost(
                    response_data, max_cost_for_model
                )
                if cost_data:
                    if unit == "msat":
                        refund_amount = amount - cost_data.total_msats
                    elif unit == "sat":
                        refund_amount = amount - (cost_data.total_msats + 999) // 1000
                    else:
                        raise ValueError(f"Invalid unit: {unit}")

                    if refund_amount > 0:
                        logger.info(
                            "Processing refund for streaming response",
                            extra={
                                "original_amount": amount,
                                "cost_msats": cost_data.total_msats,
                                "refund_amount": refund_amount,
                                "unit": unit,
                                "model": model,
                            },
                        )

                        refund_token = await self.send_refund(refund_amount, unit)
                        response_headers["X-Cashu"] = refund_token

                        logger.info(
                            "Refund processed for streaming response",
                            extra={
                                "refund_amount": refund_amount,
                                "unit": unit,
                                "refund_token_preview": refund_token[:20] + "..."
                                if len(refund_token) > 20
                                else refund_token,
                            },
                        )
                    else:
                        logger.debug(
                            "No refund needed for streaming response",
                            extra={
                                "amount": amount,
                                "cost_msats": cost_data.total_msats,
                                "model": model,
                            },
                        )
            except Exception as e:
                logger.error(
                    "Error calculating cost for streaming response",
                    extra={
                        "error": str(e),
                        "error_type": type(e).__name__,
                        "model": model,
                        "amount": amount,
                        "unit": unit,
                    },
                )

        async def generate() -> AsyncGenerator[bytes, None]:
            for line in lines:
                yield (line + "\n").encode("utf-8")

        return StreamingResponse(
            generate(),
            status_code=response.status_code,
            headers=response_headers,
            media_type="text/plain",
        )

    async def handle_x_cashu_non_streaming_response(
        self,
        content_str: str,
        response: httpx.Response,
        amount: int,
        unit: str,
        max_cost_for_model: int,
    ) -> Response:
        """Handle non-streaming response for X-Cashu payment, calculating refund if needed.

        Args:
            content_str: Response content as string
            response: Original httpx response
            amount: Payment amount received
            unit: Payment unit (sat or msat)
            max_cost_for_model: Maximum cost for the model

        Returns:
            Response with refund token in header if applicable
        """
        logger.debug(
            "Processing non-streaming response",
            extra={"amount": amount, "unit": unit, "content_length": len(content_str)},
        )

        try:
            response_json = json.loads(content_str)
            cost_data = await self.get_x_cashu_cost(response_json, max_cost_for_model)

            if not cost_data:
                logger.error(
                    "Failed to calculate cost for response",
                    extra={
                        "amount": amount,
                        "unit": unit,
                        "response_model": response_json.get("model", "unknown"),
                    },
                )
                return Response(
                    content=json.dumps(
                        {
                            "error": {
                                "message": "Error forwarding request to upstream",
                                "type": "upstream_error",
                                "code": response.status_code,
                            }
                        }
                    ),
                    status_code=response.status_code,
                    media_type="application/json",
                )

            response_headers = dict(response.headers)
            if "transfer-encoding" in response_headers:
                del response_headers["transfer-encoding"]
            if "content-encoding" in response_headers:
                del response_headers["content-encoding"]

            if unit == "msat":
                refund_amount = amount - cost_data.total_msats
            elif unit == "sat":
                refund_amount = amount - (cost_data.total_msats + 999) // 1000
            else:
                raise ValueError(f"Invalid unit: {unit}")

            logger.info(
                "Processing non-streaming response cost calculation",
                extra={
                    "original_amount": amount,
                    "cost_msats": cost_data.total_msats,
                    "refund_amount": refund_amount,
                    "unit": unit,
                    "model": response_json.get("model", "unknown"),
                },
            )

            if refund_amount > 0:
                refund_token = await self.send_refund(refund_amount, unit)
                response_headers["X-Cashu"] = refund_token

                logger.info(
                    "Refund processed for non-streaming response",
                    extra={
                        "refund_amount": refund_amount,
                        "unit": unit,
                        "refund_token_preview": refund_token[:20] + "..."
                        if len(refund_token) > 20
                        else refund_token,
                    },
                )

            return Response(
                content=content_str,
                status_code=response.status_code,
                headers=response_headers,
                media_type="application/json",
            )
        except json.JSONDecodeError as e:
            logger.error(
                "Failed to parse JSON from upstream response",
                extra={
                    "error": str(e),
                    "content_preview": content_str[:200] + "..."
                    if len(content_str) > 200
                    else content_str,
                    "amount": amount,
                    "unit": unit,
                },
            )

            from .wallet import send_token

            emergency_refund = amount
            refund_token = await send_token(emergency_refund, unit=unit)
            response.headers["X-Cashu"] = refund_token

            logger.warning(
                "Emergency refund issued due to JSON parse error",
                extra={
                    "original_amount": amount,
                    "refund_amount": emergency_refund,
                    "deduction": 60,
                },
            )

            return Response(
                content=content_str,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type="application/json",
            )

    async def handle_x_cashu_chat_completion(
        self, response: httpx.Response, amount: int, unit: str, max_cost_for_model: int
    ) -> StreamingResponse | Response:
        """Handle chat completion response for X-Cashu payment, detecting streaming vs non-streaming.

        Args:
            response: Response from upstream
            amount: Payment amount received
            unit: Payment unit (sat or msat)
            max_cost_for_model: Maximum cost for the model

        Returns:
            StreamingResponse or Response depending on response type
        """
        logger.debug(
            "Handling chat completion response",
            extra={"amount": amount, "unit": unit, "status_code": response.status_code},
        )

        try:
            content = await response.aread()
            content_str = (
                content.decode("utf-8") if isinstance(content, bytes) else content
            )
            is_streaming = content_str.startswith("data:") or "data:" in content_str

            logger.debug(
                "Chat completion response analysis",
                extra={
                    "is_streaming": is_streaming,
                    "content_length": len(content_str),
                    "amount": amount,
                    "unit": unit,
                },
            )

            if is_streaming:
                return await self.handle_x_cashu_streaming_response(
                    content_str, response, amount, unit, max_cost_for_model
                )
            else:
                return await self.handle_x_cashu_non_streaming_response(
                    content_str, response, amount, unit, max_cost_for_model
                )

        except Exception as e:
            logger.error(
                "Error processing chat completion response",
                extra={
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "amount": amount,
                    "unit": unit,
                },
            )
            return StreamingResponse(
                response.aiter_bytes(),
                status_code=response.status_code,
                headers=dict(response.headers),
            )

    async def forward_x_cashu_request(
        self,
        request: Request,
        path: str,
        headers: dict,
        amount: int,
        unit: str,
        max_cost_for_model: int,
    ) -> Response | StreamingResponse:
        """Forward request paid with X-Cashu token to upstream service.

        Args:
            request: Original FastAPI request
            path: Request path
            headers: Prepared headers for upstream
            amount: Payment amount from X-Cashu token
            unit: Payment unit (sat or msat)
            max_cost_for_model: Maximum cost for the model

        Returns:
            Response or StreamingResponse with refund if applicable
        """
        if path.startswith("v1/"):
            path = path.replace("v1/", "")

        url = f"{self.base_url}/{path}"

        request_body = await request.body()
        transformed_body = self.prepare_request_body(request_body)

        logger.debug(
            "Forwarding request to upstream",
            extra={
                "url": url,
                "method": request.method,
                "path": path,
                "amount": amount,
                "unit": unit,
            },
        )

        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(retries=1),
            timeout=None,
        ) as client:
            try:
                response = await client.send(
                    client.build_request(
                        request.method,
                        url,
                        headers=headers,
                        content=transformed_body if transformed_body else request_body,
                        params=self.prepare_params(path, request.query_params),
                    ),
                    stream=True,
                )

                logger.debug(
                    "Received upstream response",
                    extra={
                        "status_code": response.status_code,
                        "path": path,
                        "response_headers": dict(response.headers),
                    },
                )

                if response.status_code != 200:
                    logger.warning(
                        "Upstream request failed, processing refund",
                        extra={
                            "status_code": response.status_code,
                            "path": path,
                            "amount": amount,
                            "unit": unit,
                        },
                    )

                    refund_token = await self.send_refund(amount - 60, unit)

                    logger.info(
                        "Refund processed for failed upstream request",
                        extra={
                            "status_code": response.status_code,
                            "refund_amount": amount,
                            "unit": unit,
                            "refund_token_preview": refund_token[:20] + "..."
                            if len(refund_token) > 20
                            else refund_token,
                        },
                    )

                    error_response = Response(
                        content=json.dumps(
                            {
                                "error": {
                                    "message": "Error forwarding request to upstream",
                                    "type": "upstream_error",
                                    "code": response.status_code,
                                    "refund_token": refund_token,
                                }
                            }
                        ),
                        status_code=response.status_code,
                        media_type="application/json",
                    )
                    error_response.headers["X-Cashu"] = refund_token
                    return error_response

                if path.endswith("chat/completions"):
                    logger.debug(
                        "Processing chat completion response",
                        extra={"path": path, "amount": amount, "unit": unit},
                    )

                    result = await self.handle_x_cashu_chat_completion(
                        response, amount, unit, max_cost_for_model
                    )
                    background_tasks = BackgroundTasks()
                    background_tasks.add_task(response.aclose)
                    result.background = background_tasks
                    return result

                background_tasks = BackgroundTasks()
                background_tasks.add_task(response.aclose)
                background_tasks.add_task(client.aclose)

                logger.debug(
                    "Streaming non-chat response",
                    extra={"path": path, "status_code": response.status_code},
                )

                return StreamingResponse(
                    response.aiter_bytes(),
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    background=background_tasks,
                )
            except Exception as exc:
                tb = traceback.format_exc()
                logger.error(
                    "Unexpected error in upstream forwarding",
                    extra={
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                        "method": request.method,
                        "url": url,
                        "path": path,
                        "query_params": dict(request.query_params),
                        "traceback": tb,
                    },
                )
                return create_error_response(
                    "internal_error",
                    "An unexpected server error occurred",
                    500,
                    request=request,
                )

    async def handle_x_cashu(
        self, request: Request, x_cashu_token: str, path: str, max_cost_for_model: int
    ) -> Response | StreamingResponse:
        """Handle request with X-Cashu token payment, redeeming token and forwarding request.

        Args:
            request: Original FastAPI request
            x_cashu_token: X-Cashu token from request header
            path: Request path
            max_cost_for_model: Maximum cost for the model

        Returns:
            Response or StreamingResponse from upstream with refund if applicable
        """
        from .wallet import recieve_token

        logger.info(
            "Processing X-Cashu payment request",
            extra={
                "path": path,
                "method": request.method,
                "token_preview": x_cashu_token[:20] + "..."
                if len(x_cashu_token) > 20
                else x_cashu_token,
            },
        )

        try:
            headers = dict(request.headers)
            amount, unit, mint = await recieve_token(x_cashu_token)
            headers = self.prepare_headers(dict(request.headers))

            logger.info(
                "X-Cashu token redeemed successfully",
                extra={"amount": amount, "unit": unit, "path": path, "mint": mint},
            )

            return await self.forward_x_cashu_request(
                request,
                path,
                headers,
                amount,
                unit,
                max_cost_for_model,
            )
        except Exception as e:
            error_message = str(e)
            logger.error(
                "X-Cashu payment request failed",
                extra={
                    "error": error_message,
                    "error_type": type(e).__name__,
                    "path": path,
                    "method": request.method,
                },
            )

            if "already spent" in error_message.lower():
                return create_error_response(
                    "token_already_spent",
                    "The provided CASHU token has already been spent",
                    400,
                    request=request,
                    token=x_cashu_token,
                )

            if "invalid token" in error_message.lower():
                return create_error_response(
                    "invalid_token",
                    "The provided CASHU token is invalid",
                    400,
                    request=request,
                    token=x_cashu_token,
                )

            if "mint error" in error_message.lower():
                return create_error_response(
                    "mint_error",
                    f"CASHU mint error: {error_message}",
                    422,
                    request=request,
                    token=x_cashu_token,
                )

            return create_error_response(
                "cashu_error",
                f"CASHU token processing failed: {error_message}",
                400,
                request=request,
                token=x_cashu_token,
            )

    def _apply_provider_fee_to_model(self, model: Model) -> Model:
        """Apply provider fee to model's USD pricing and calculate max costs.

        Args:
            model: Model object to update

        Returns:
            Model with provider fee applied to pricing and max costs calculated
        """
        from .payment.models import Pricing, _calculate_usd_max_costs

        adjusted_pricing = Pricing.parse_obj(
            {k: v * self.provider_fee for k, v in model.pricing.dict().items()}
        )

        temp_model = Model(
            id=model.id,
            name=model.name,
            created=model.created,
            description=model.description,
            context_length=model.context_length,
            architecture=model.architecture,
            pricing=adjusted_pricing,
            sats_pricing=None,
            per_request_limits=model.per_request_limits,
            top_provider=model.top_provider,
            enabled=model.enabled,
            upstream_provider_id=model.upstream_provider_id,
            canonical_slug=model.canonical_slug,
        )

        (
            adjusted_pricing.max_prompt_cost,
            adjusted_pricing.max_completion_cost,
            adjusted_pricing.max_cost,
        ) = _calculate_usd_max_costs(temp_model)

        return Model(
            id=model.id,
            name=model.name,
            created=model.created,
            description=model.description,
            context_length=model.context_length,
            architecture=model.architecture,
            pricing=adjusted_pricing,
            sats_pricing=model.sats_pricing,
            per_request_limits=model.per_request_limits,
            top_provider=model.top_provider,
            enabled=model.enabled,
            upstream_provider_id=model.upstream_provider_id,
            canonical_slug=model.canonical_slug,
        )

    async def fetch_models(self) -> list[Model]:
        """Fetch available models from upstream API and update cache.

        Returns:
            List of Model objects with pricing
        """
        logger.debug(f"Fetching models for {self.upstream_name or self.base_url}")
        return []

    async def refresh_models_cache(self) -> None:
        """Refresh the in-memory models cache from upstream API."""
        try:
            from .payment.models import _update_model_sats_pricing
            from .payment.price import sats_usd_price

            models = await self.fetch_models()
            models_with_fees = [self._apply_provider_fee_to_model(m) for m in models]

            try:
                sats_to_usd = sats_usd_price()
                self._models_cache = [
                    _update_model_sats_pricing(m, sats_to_usd) for m in models_with_fees
                ]
            except Exception:
                self._models_cache = models_with_fees

            self._models_by_id = {m.id: m for m in self._models_cache}
            logger.info(
                f"Refreshed models cache for {self.upstream_name or self.base_url}",
                extra={"model_count": len(models)},
            )
        except Exception as e:
            logger.error(
                f"Failed to refresh models cache for {self.upstream_name or self.base_url}",
                extra={"error": str(e), "error_type": type(e).__name__},
            )

    def get_cached_models(self) -> list[Model]:
        """Get cached models for this provider.

        Returns:
            List of cached Model objects
        """
        return self._models_cache

    def get_cached_model_by_id(self, model_id: str) -> Model | None:
        """Get a specific cached model by ID.

        Args:
            model_id: Model identifier

        Returns:
            Model object or None if not found
        """
        return self._models_by_id.get(model_id)


class OpenAIUpstreamProvider(UpstreamProvider):
    """Upstream provider specifically configured for OpenAI API."""

    def __init__(self, api_key: str, provider_fee: float = 1.01):
        self.upstream_name = "openai"
        super().__init__(
            base_url="https://api.openai.com/v1",
            api_key=api_key,
            provider_fee=provider_fee,
        )

    def transform_model_name(self, model_id: str) -> str:
        """Strip 'openai/' prefix for OpenAI API compatibility."""
        return model_id.removeprefix("openai/")

    async def fetch_models(self) -> list[Model]:
        """Fetch OpenAI models from OpenRouter API filtered by openai source."""
        models_data = await async_fetch_openrouter_models(source_filter="openai")
        return [Model(**model) for model in models_data]  # type: ignore


class AnthropicUpstreamProvider(UpstreamProvider):
    """Upstream provider specifically configured for Anthropic API."""

    def __init__(self, api_key: str, provider_fee: float = 1.01):
        self.upstream_name = "anthropic"
        super().__init__(
            base_url="https://api.anthropic.com/v1",
            api_key=api_key,
            provider_fee=provider_fee,
        )

    def transform_model_name(self, model_id: str) -> str:
        """Strip 'anthropic/' prefix for Anthropic API compatibility."""
        return model_id.removeprefix("anthropic/")

    async def fetch_models(self) -> list[Model]:
        """Fetch Anthropic models from OpenRouter API filtered by anthropic source."""
        models_data = await async_fetch_openrouter_models(source_filter="anthropic")
        return [Model(**model) for model in models_data]  # type: ignore


class AzureUpstreamProvider(UpstreamProvider):
    """Upstream provider specifically configured for Azure OpenAI Service."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_version: str,
        provider_fee: float = 1.01,
    ):
        """Initialize Azure provider with API key and version.

        Args:
            base_url: Azure OpenAI endpoint base URL
            api_key: Azure OpenAI API key for authentication
            api_version: Azure OpenAI API version (e.g., "2024-02-15-preview")
            provider_fee: Provider fee multiplier (default 1.01 for 1% fee)
        """
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            provider_fee=provider_fee,
        )
        self.api_version = api_version

    def prepare_params(
        self, path: str, query_params: Mapping[str, str] | None
    ) -> Mapping[str, str]:
        """Prepare query parameters for Azure OpenAI, adding API version.

        Args:
            path: Request path
            query_params: Original query parameters from the client

        Returns:
            Query parameters dict with Azure API version added for chat completions
        """
        params = dict(query_params or {})
        if path.endswith("chat/completions"):
            params["api-version"] = self.api_version
        return params


class OpenRouterUpstreamProvider(UpstreamProvider):
    """Upstream provider specifically configured for OpenRouter API."""

    def __init__(self, api_key: str, provider_fee: float = 1.06):
        """Initialize OpenRouter provider with API key.

        Args:
            api_key: OpenRouter API key for authentication
            provider_fee: Provider fee multiplier (default 1.06 for 6% fee)
        """
        self.upstream_name = "openrouter"
        super().__init__(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            provider_fee=provider_fee,
        )

    async def fetch_models(self) -> list[Model]:
        """Fetch all OpenRouter models."""
        models_data = await async_fetch_openrouter_models()
        return [Model(**model) for model in models_data]  # type: ignore
