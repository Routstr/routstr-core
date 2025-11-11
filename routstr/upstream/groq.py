from ..payment.models import Model, async_fetch_openrouter_models
from .base import BaseUpstreamProvider


class GroqUpstreamProvider(BaseUpstreamProvider):
    """Upstream provider specifically configured for Groq API."""

    upstream_name = "groq"
    base_url = "https://api.groq.com/openai/v1"
    platform_url = "https://console.groq.com/keys"

    def __init__(self, api_key: str, provider_fee: float = 1.01):
        super().__init__(
            base_url=self.base_url, api_key=api_key, provider_fee=provider_fee
        )

    def transform_model_name(self, model_id: str) -> str:
        """Strip 'groq/' prefix for Groq API compatibility."""
        return model_id.removeprefix("groq/")

    async def fetch_models(self) -> list[Model]:
        """Fetch Groq models from OpenRouter API filtered by groq source."""
        models_data = await async_fetch_openrouter_models(source_filter="groq")
        return [Model(**model) for model in models_data]  # type: ignore
