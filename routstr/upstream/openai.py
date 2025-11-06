from ..payment.models import Model, async_fetch_openrouter_models
from .base import BaseUpstreamProvider


class OpenAIUpstreamProvider(BaseUpstreamProvider):
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
