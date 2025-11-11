from ..payment.models import Model, async_fetch_openrouter_models
from .base import BaseUpstreamProvider


class FireworksUpstreamProvider(BaseUpstreamProvider):
    """Upstream provider specifically configured for Fireworks.ai API."""

    upstream_name = "fireworks"
    base_url = "https://api.fireworks.ai/inference/v1"
    platform_url = "https://app.fireworks.ai/settings/users/api-keys"

    def __init__(self, api_key: str, provider_fee: float = 1.01):
        super().__init__(
            base_url=self.base_url, api_key=api_key, provider_fee=provider_fee
        )

    def transform_model_name(self, model_id: str) -> str:
        """Strip 'fireworks/' prefix for Fireworks API compatibility."""
        return model_id.removeprefix("fireworks/")

    async def fetch_models(self) -> list[Model]:
        """Fetch Fireworks models from OpenRouter API filtered by fireworks source."""
        models_data = await async_fetch_openrouter_models(source_filter="fireworks")
        return [Model(**model) for model in models_data]  # type: ignore
