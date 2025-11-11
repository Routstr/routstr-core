from ..payment.models import Model, async_fetch_openrouter_models
from .base import BaseUpstreamProvider


class OpenRouterUpstreamProvider(BaseUpstreamProvider):
    """Upstream provider specifically configured for OpenRouter API."""

    def __init__(self, api_key: str, provider_fee: float = 1.06):
        """Initialize OpenRouter provider with API key.

        Args:
            api_key: OpenRouter API key for authentication
            provider_fee: Provider fee multiplier (default 1.06 for 6% fee)
        """
        self.upstream_name = "openrouter"
        super().__init__(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            provider_fee=provider_fee,
        )

    async def fetch_models(self) -> list[Model]:
        """Fetch all OpenRouter models."""
        models_data = await async_fetch_openrouter_models()
        return [Model(**model) for model in models_data]  # type: ignore
