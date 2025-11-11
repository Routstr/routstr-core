from typing import TYPE_CHECKING

from ..payment.models import Model, async_fetch_openrouter_models
from .base import BaseUpstreamProvider

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow


class GroqUpstreamProvider(BaseUpstreamProvider):
    """Upstream provider specifically configured for Groq API."""

    provider_type = "groq"
    default_base_url = "https://api.groq.com/openai/v1"
    platform_url = "https://console.groq.com/keys"

    def __init__(self, api_key: str, provider_fee: float = 1.01):
        super().__init__(
            base_url=self.default_base_url, api_key=api_key, provider_fee=provider_fee
        )

    @classmethod
    def from_db_row(cls, provider_row: "UpstreamProviderRow") -> "GroqUpstreamProvider":
        return cls(
            api_key=provider_row.api_key,
            provider_fee=provider_row.provider_fee,
        )

    @classmethod
    def get_provider_metadata(cls) -> dict[str, object]:
        return {
            "id": cls.provider_type,
            "name": "Groq",
            "default_base_url": cls.default_base_url,
            "fixed_base_url": True,
            "platform_url": cls.platform_url,
        }

    def transform_model_name(self, model_id: str) -> str:
        """Strip 'groq/' prefix for Groq API compatibility."""
        return model_id.removeprefix("groq/")

    async def fetch_models(self) -> list[Model]:
        """Fetch Groq models from OpenRouter API filtered by groq source."""
        models_data = await async_fetch_openrouter_models(source_filter="groq")
        return [Model(**model) for model in models_data]  # type: ignore
