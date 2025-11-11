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
        """Strip 'anthropic/' prefix for Anthropic API compatibility and transform model names."""
        if model_id.startswith("anthropic/"):
            model_id = model_id[len("anthropic/") :]
        fixed_transforms = {
            "claude-haiku-4.5": "claude-haiku-4-5-20251001",
            "claude-sonnet-4.5": "claude-sonnet-4-5-20250929",
            "claude-opus-4.1": "claude-opus-4-1-20250805",
            "claude-opus-4": "claude-opus-4-20250514",
            "claude-sonnet-4": "claude-sonnet-4-20250514",
            "claude-3.5-haiku": "claude-3-5-haiku-20241022",
            "claude-3-haiku": "claude-3-haiku-20240307",
            "claude-haiku-4-5": "claude-haiku-4-5-20251001",
            "claude-sonnet-4-5": "claude-sonnet-4-5-20250929",
            "claude-opus-4-1": "claude-opus-4-1-20250805",
            "claude-3-5-haiku": "claude-3-5-haiku-20241022",
        }
        if model_id in fixed_transforms:
            model_id = fixed_transforms[model_id]
        return model_id

    async def fetch_models(self) -> list[Model]:
        """Fetch Anthropic models from OpenRouter API filtered by anthropic source."""
        models_data = await async_fetch_openrouter_models(source_filter="anthropic")
        models = [Model(**model) for model in models_data]  # type: ignore
        for model in models:
            model.alias_ids = [self.transform_model_name(model.id)]
        return models
