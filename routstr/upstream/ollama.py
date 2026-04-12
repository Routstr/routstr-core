from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from .base import BaseUpstreamProvider

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
    def from_db_row(
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

    def get_request_base_url(self, path: str, model_obj: Model | None = None) -> str:
        """Route proxy traffic through Ollama's OpenAI-compatible /v1 endpoint."""
        return f"{self.base_url.rstrip('/')}/v1"

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
