from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core.settings import Settings


from ..core import get_logger
from ..core.db import AsyncSession, ModelRow, UpstreamProviderRow, create_session
from ..payment.models import Model
from .anthropic import AnthropicUpstreamProvider
from .azure import AzureUpstreamProvider
from .base import BaseUpstreamProvider
from .generic import GenericUpstreamProvider
from .ollama import OllamaUpstreamProvider
from .openai import OpenAIUpstreamProvider
from .openrouter import OpenRouterUpstreamProvider

logger = get_logger(__name__)


def resolve_model_alias(
    model_id: str, canonical_slug: str | None = None, alias_ids: list[str] | None = None
) -> list[str]:
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

    if alias_ids:
        aliases.extend(alias_ids)

    return aliases


async def get_all_models_with_overrides(
    upstreams: list[BaseUpstreamProvider],
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

    from ..payment.models import _row_to_model

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


async def refresh_upstreams_models_periodically(
    upstreams: list[BaseUpstreamProvider],
) -> None:
    """Background task to periodically refresh models cache for all providers.

    Args:
        upstreams: List of upstream provider instances
    """
    import asyncio
    import random

    from ..core.settings import settings

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


async def init_upstreams() -> list[BaseUpstreamProvider]:
    """Initialize upstream providers from database.

    Seeds database with providers from settings if empty, then loads and instantiates
    provider instances from database records, and refreshes their models cache.
    """
    from sqlmodel import select

    from ..core.settings import settings

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

        upstreams: list[BaseUpstreamProvider] = []
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

    from ..core.settings import settings

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

    ollama_base_url = os.environ.get("OLLAMA_BASE_URL")
    if ollama_base_url:
        result = await session.exec(
            select(UpstreamProviderRow).where(
                UpstreamProviderRow.base_url == ollama_base_url
            )
        )
        if not result.first():
            providers_to_add.append(
                UpstreamProviderRow(
                    provider_type="ollama",
                    base_url=ollama_base_url,
                    api_key=os.environ.get("OLLAMA_API_KEY", ""),
                    enabled=True,
                )
            )
            seeded_base_urls.add(ollama_base_url)

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


def _instantiate_provider(
    provider_row: UpstreamProviderRow,
) -> BaseUpstreamProvider | None:
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
        elif provider_row.provider_type == "ollama":
            return OllamaUpstreamProvider(
                provider_row.base_url, provider_row.api_key, provider_row.provider_fee
            )
        elif provider_row.provider_type == "generic":
            return GenericUpstreamProvider(
                provider_row.base_url,
                provider_row.api_key,
                provider_row.provider_fee,
                provider_row.provider_type,
            )
        elif provider_row.provider_type == "custom":
            return BaseUpstreamProvider(
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
