from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from .base import BaseUpstreamProvider

if TYPE_CHECKING:
    from ..payment.models import Model

from ..core.logging import get_logger

logger = get_logger(__name__)


class GenericUpstreamProvider(BaseUpstreamProvider):
    """Generic upstream provider that can fetch models from any OpenAI-compatible API."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        provider_fee: float = 1.01,
        upstream_name: str | None = None,
    ):
        """Initialize generic provider.

        Args:
            base_url: Base URL of the upstream API endpoint
            api_key: Optional API key for authentication
            provider_fee: Provider fee multiplier (default 1.01 for 1% fee)
            upstream_name: Optional name for the upstream provider
        """
        self.upstream_name = upstream_name or "generic"
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            provider_fee=provider_fee,
        )

    async def fetch_models(self) -> list[Model]:
        """Fetch models from upstream API using /models endpoint."""
        from ..payment.models import Architecture, Model, Pricing, TopProvider

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                headers = {}
                if self.api_key:
                    headers["Authorization"] = f"Bearer {self.api_key}"

                response = await client.get(f"{self.base_url}/models", headers=headers)
                response.raise_for_status()
                data = response.json()

                models_list = []
                for model_data in data.get("data", []):
                    model_id = model_data.get("id", "")
                    if not model_id:
                        continue

                    model_name = model_data.get("name", model_id)
                    created = model_data.get("created", 0)
                    owned_by = model_data.get("owned_by", "unknown")
                    model_spec = model_data.get("model_spec", {})

                    context_length = 4096
                    if model_spec.get("availableContextTokens"):
                        context_length = model_spec["availableContextTokens"]
                    elif any(
                        pattern in model_id.lower() for pattern in ["32k", "32000"]
                    ):
                        context_length = 32768
                    elif any(
                        pattern in model_id.lower() for pattern in ["16k", "16000"]
                    ):
                        context_length = 16384
                    elif any(pattern in model_id.lower() for pattern in ["8k", "8000"]):
                        context_length = 8192
                    elif "gpt-4" in model_id.lower():
                        context_length = 8192
                    elif "claude" in model_id.lower():
                        context_length = 200000

                    pricing_info = model_spec.get("pricing", {})
                    input_pricing = pricing_info.get("input", {})
                    output_pricing = pricing_info.get("output", {})

                    prompt_price = input_pricing.get("usd", 0.001) / 1000000
                    completion_price = output_pricing.get("usd", 0.001) / 1000000

                    capabilities = model_spec.get("capabilities", {})
                    input_modalities = ["text"]
                    output_modalities = ["text"]

                    if capabilities.get("supportsVision", False):
                        input_modalities.append("image")

                    modality = "text"
                    if capabilities.get("supportsVision", False):
                        modality = "text->text"

                    spec_name = model_spec.get("name", model_name)
                    description = f"{spec_name}"
                    if owned_by != "unknown":
                        description += f" via {owned_by}"

                    models_list.append(
                        Model(
                            id=model_id,
                            name=spec_name,
                            created=created,
                            description=description,
                            context_length=context_length,
                            architecture=Architecture(
                                modality=modality,
                                input_modalities=input_modalities,
                                output_modalities=output_modalities,
                                tokenizer="unknown",
                                instruct_type=None,
                            ),
                            pricing=Pricing(
                                prompt=prompt_price,
                                completion=completion_price,
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
                    f"Fetched {len(models_list)} models from {self.upstream_name}",
                    extra={"model_count": len(models_list), "base_url": self.base_url},
                )
                return models_list

        except Exception as e:
            logger.error(
                f"Failed to fetch models from {self.upstream_name} API: {e}",
                extra={
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "base_url": self.base_url,
                },
            )
            return []
