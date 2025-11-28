from typing import TYPE_CHECKING

from ..models.models import Model, async_fetch_openrouter_models
from .base import BaseUpstreamProvider

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow


class PerplexityUpstreamProvider(BaseUpstreamProvider):
    """Upstream provider specifically configured for Perplexity API."""

    provider_type = "perplexity"
    default_base_url = "https://api.perplexity.ai/"
    platform_url = "https://www.perplexity.ai/account/api/keys"

    def __init__(self, api_key: str, provider_fee: float = 1.01):
        super().__init__(
            base_url=self.default_base_url,
            api_key=api_key,
            provider_fee=provider_fee,
        )

    @classmethod
    def from_db_row(
        cls, provider_row: "UpstreamProviderRow"
    ) -> "PerplexityUpstreamProvider":
        return cls(
            api_key=provider_row.api_key,
            provider_fee=provider_row.provider_fee,
        )

    @classmethod
    def get_provider_metadata(cls) -> dict[str, object]:
        return {
            "id": cls.provider_type,
            "name": "Perplexity",
            "default_base_url": cls.default_base_url,
            "fixed_base_url": True,
            "platform_url": cls.platform_url,
        }

    def transform_model_name(self, model_id: str) -> str:
        """Strip 'perplexity/' prefix for Perplexity API compatibility."""
        return model_id.removeprefix("perplexity/")

    async def fetch_models(self) -> list[Model]:
        """Fetch Perplexity models from OpenRouter API filtered by perplexity source."""
        models_data = await async_fetch_openrouter_models(source_filter="perplexity")
        return [Model(**model) for model in models_data]  # type: ignore
