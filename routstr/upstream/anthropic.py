from ..payment.models import Model, async_fetch_openrouter_models
from .base import BaseUpstreamProvider


class AnthropicUpstreamProvider(BaseUpstreamProvider):
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
