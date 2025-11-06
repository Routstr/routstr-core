from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from .base import BaseUpstreamProvider

if TYPE_CHECKING:
    from ..core.db import ApiKey, AsyncSession
    from ..payment.models import Model

from ..core.logging import get_logger

logger = get_logger(__name__)


class OllamaUpstreamProvider(BaseUpstreamProvider):
    """Upstream provider specifically configured for Ollama API."""

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
        self.upstream_name = "ollama"
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            provider_fee=provider_fee,
        )

    def transform_model_name(self, model_id: str) -> str:
        """Strip 'ollama/' prefix for Ollama API compatibility."""
        return model_id.removeprefix("ollama/")

    async def forward_request(
        self,
        request: Request,
        path: str,
        headers: dict,
        request_body: bytes | None,
        key: ApiKey,
        max_cost_for_model: int,
        session: AsyncSession,
        model_obj: Model,
    ) -> Response | StreamingResponse:
        """Override to use OpenAI-compatible endpoint for proxy requests."""
        if path.startswith("v1/"):
            path = path.replace("v1/", "")

        original_base_url = self.base_url
        self.base_url = f"{self.base_url}/v1"

        try:
            result = await super().forward_request(
                request,
                path,
                headers,
                request_body,
                key,
                max_cost_for_model,
                session,
                model_obj,
            )
            return result
        finally:
            self.base_url = original_base_url

    async def fetch_models(self) -> list[Model]:
        """Fetch models from Ollama API using /api/tags endpoint."""
        from ..payment.models import Architecture, Model, Pricing, TopProvider

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                data = response.json()

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
                                prompt=0.000003,
                                completion=0.000003,
                                request=0.0,
                                image=0.0,
                                web_search=0.0,
                                internal_reasoning=0.0,
                                max_prompt_cost=0.001,
                                max_completion_cost=0.001,
                                max_cost=0.001,
                            ),
                            sats_pricing=None,
                            per_request_limits=None,
                            top_provider=TopProvider(
                                context_length=context_length,
                                max_completion_tokens=context_length // 2,
                                is_moderated=False,
                            ),
                            enabled=True,
                            upstream_provider_id=None,
                            canonical_slug=None,
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
