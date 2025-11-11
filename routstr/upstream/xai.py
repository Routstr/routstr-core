from ..payment.models import Model, async_fetch_openrouter_models
from .base import BaseUpstreamProvider


class XAIUpstreamProvider(BaseUpstreamProvider):
    """Upstream provider specifically configured for XAI API."""

    upstream_name = "xai"
    base_url = "https://api.x.ai/v1"
    platform_url = "https://accounts.x.ai/sign-up"

    def __init__(self, api_key: str, provider_fee: float = 1.01):
        super().__init__(
            base_url=self.base_url, api_key=api_key, provider_fee=provider_fee
        )

    def transform_model_name(self, model_id: str) -> str:
        """Strip 'xai/' prefix for XAI API compatibility."""
        return model_id.removeprefix("xai/")

    async def fetch_models(self) -> list[Model]:
        """Fetch XAI models from OpenRouter API filtered by xai source."""
        models_data = await async_fetch_openrouter_models(source_filter="xai")
        return [Model(**model) for model in models_data]  # type: ignore
