from ..payment.models import Model, async_fetch_openrouter_models
from .base import BaseUpstreamProvider


class PerplexityUpstreamProvider(BaseUpstreamProvider):
    """Upstream provider specifically configured for OpenAI API."""

    upstream_name = "perplexity"
    base_url = "https://api.perplexity.ai/"  # without v1
    platform_url = "https://www.perplexity.ai/account/api/keys"

    def __init__(self, api_key: str, provider_fee: float = 1.01):
        super().__init__(
            base_url=self.base_url,
            api_key=api_key,
            provider_fee=provider_fee,
        )

    def transform_model_name(self, model_id: str) -> str:
        """Strip 'perplexity/' prefix for Perplexity API compatibility."""
        return model_id.removeprefix("perplexity/")

    async def fetch_models(self) -> list[Model]:
        """Fetch Perplexity models from OpenRouter API filtered by perplexity source."""
        models_data = await async_fetch_openrouter_models(source_filter="perplexity")
        return [Model(**model) for model in models_data]  # type: ignore
