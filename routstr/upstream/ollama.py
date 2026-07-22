from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from .base import BaseUpstreamProvider
from .pricing_resolver import FallbackPricingResolver, ResolvedPricing

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow
    from ..payment.models import Model

from ..core.logging import get_logger

logger = get_logger(__name__)


class OllamaUpstreamProvider(BaseUpstreamProvider):
    """Upstream provider specifically configured for Ollama API."""

    provider_type = "ollama"
    default_base_url = "http://localhost:11434"
    platform_url = None
    litellm_provider_prefix = "ollama_chat/"

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        api_key: str = "",
        provider_fee: float = 1.01,
    ):
        """Initialize Ollama provider.

        Args:
            base_url: Ollama API base URL (default http://localhost:11434)
            api_key: Optional API key (Ollama typically doesn't require one)
            provider_fee: Provider fee multiplier (default 1.01 for 1% fee)
        """
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            provider_fee=provider_fee,
        )

    @classmethod
    def _build_from_row(
        cls, provider_row: "UpstreamProviderRow"
    ) -> "OllamaUpstreamProvider":
        return cls(
            base_url=provider_row.base_url,
            api_key=provider_row.api_key,
            provider_fee=provider_row.provider_fee,
        )

    @classmethod
    def get_provider_metadata(cls) -> dict[str, object]:
        return {
            "id": cls.provider_type,
            "name": "Ollama",
            "default_base_url": cls.default_base_url,
            "fixed_base_url": False,
            "platform_url": cls.platform_url,
        }

    def transform_model_name(self, model_id: str) -> str:
        """Strip 'ollama/' prefix for Ollama API compatibility."""
        return model_id.removeprefix("ollama/")

    def get_request_base_url(
        self, path: str, model_obj: Model | None = None
    ) -> str:
        """Route proxy traffic through Ollama's OpenAI-compatible /v1 endpoint."""
        return f"{self.base_url.rstrip('/')}/v1"

    async def fetch_models(self) -> list[Model]:
        """Fetch models from Ollama API using /api/tags endpoint."""
        from ..payment.models import (
            Architecture,
            Model,
            Pricing,
            TopProvider,
            pricing_metadata,
        )

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                data = response.json()

                resolver = FallbackPricingResolver()
                models_list = []
                for model_data in data.get("models", []):
                    model_name = model_data.get("name", "")
                    if not model_name:
                        continue

                    details = model_data.get("details", {})
                    parameter_size = details.get("parameter_size", "")

                    context_length = 4096
                    if (
                        "70b" in parameter_size.lower()
                        or "72b" in parameter_size.lower()
                    ):
                        context_length = 8192
                    elif "13b" in parameter_size.lower():
                        context_length = 4096
                    elif "7b" in parameter_size.lower():
                        context_length = 4096
                    elif "3b" in parameter_size.lower():
                        context_length = 2048
                    elif "1b" in parameter_size.lower():
                        context_length = 2048

                    model_family = details.get("family", "unknown")
                    model_format = details.get("format", "unknown")

                    description = f"Ollama {model_family} model"
                    if parameter_size:
                        description += f" ({parameter_size})"

                    # Ollama's /api/tags carries no pricing, so we never claim
                    # `native`: resolve through the shared litellm→OpenRouter
                    # chain and wear that source, or fail closed as `unresolved`
                    # (imported disabled) when nothing can price the model.
                    resolved = await resolver.resolve(model_name)
                    if resolved is None:
                        logger.warning(
                            f"No pricing source resolved for Ollama model "
                            f"'{model_name}'; importing it disabled",
                            extra={
                                "model_id": model_name,
                                "base_url": self.base_url,
                            },
                        )
                        resolved = ResolvedPricing(
                            prompt=0.0,
                            completion=0.0,
                            context_length=None,
                            source="unresolved",
                        )
                        enabled = False
                    else:
                        enabled = True

                    models_list.append(
                        Model(
                            id=model_name,
                            name=model_name.replace(":", " "),
                            created=0,
                            description=description,
                            context_length=context_length,
                            architecture=Architecture(
                                modality="text",
                                input_modalities=["text"],
                                output_modalities=["text"],
                                tokenizer=model_format,
                                instruct_type=None,
                            ),
                            pricing=Pricing(
                                prompt=resolved.prompt,
                                completion=resolved.completion,
                                request=0.0,
                                image=0.0,
                                web_search=0.0,
                                internal_reasoning=0.0,
                                input_cache_read=resolved.input_cache_read,
                                input_cache_write=resolved.input_cache_write,
                            ),
                            sats_pricing=None,
                            per_request_limits=None,
                            top_provider=TopProvider(
                                context_length=context_length,
                                max_completion_tokens=context_length // 2,
                                is_moderated=False,
                            ),
                            enabled=enabled,
                            upstream_provider_id=None,
                            canonical_slug=None,
                            **pricing_metadata(resolved.source),
                        )
                    )

                logger.info(
                    f"Fetched {len(models_list)} models from Ollama",
                    extra={"model_count": len(models_list), "base_url": self.base_url},
                )
                return models_list

        except Exception as e:
            logger.error(
                f"Failed to fetch models from Ollama API: {e}",
                extra={
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "base_url": self.base_url,
                },
            )
            return []

    async def refresh_models_cache(self) -> None:
        """Refresh the in-memory models cache from upstream API."""
        try:
            from ..payment.models import _update_model_sats_pricing
            from ..payment.price import sats_usd_price

            models = await self.fetch_models()
            models_with_fees = [self._apply_provider_fee_to_model(m) for m in models]

            try:
                sats_to_usd = sats_usd_price()
                self._models_cache = [
                    _update_model_sats_pricing(m, sats_to_usd) for m in models_with_fees
                ]
            except Exception:
                self._models_cache = models_with_fees

            self._models_by_id = {m.forwarded_model_id or m.id: m for m in self._models_cache}
            logger.info(
                f"Refreshed models cache for {self.base_url}",
                extra={"model_count": len(models)},
            )
        except Exception as e:
            logger.error(
                f"Failed to refresh models cache for {self.base_url}",
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

    def _apply_provider_fee_to_model(self, model: Model) -> Model:
        """Apply provider fee to model's USD pricing and calculate max costs.

        Args:
            model: Model object to update

        Returns:
            Model with provider fee applied to pricing and max costs calculated
        """
        from ..payment.models import Model, Pricing, _calculate_usd_max_costs

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
            pricing_source=model.pricing_source,
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
            pricing_source=model.pricing_source,
        )
