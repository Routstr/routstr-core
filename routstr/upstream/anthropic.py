import json
from typing import TYPE_CHECKING

from ..payment.models import Model, async_fetch_openrouter_models
from .base import BaseUpstreamProvider

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow


class AnthropicUpstreamProvider(BaseUpstreamProvider):
    """Upstream provider specifically configured for Anthropic API."""

    provider_type = "anthropic"
    default_base_url = "https://api.anthropic.com/v1"
    platform_url = "https://console.anthropic.com/settings/keys"
    supports_anthropic_messages = True
    litellm_provider_prefix = "anthropic/"

    def __init__(
        self,
        api_key: str,
        provider_fee: float = 1.01,
        workspace_id: str | None = None,
    ):
        super().__init__(
            base_url=self.default_base_url,
            api_key=api_key,
            provider_fee=provider_fee,
        )
        self.workspace_id = workspace_id.strip() if workspace_id else None

    @classmethod
    def _build_from_row(
        cls, provider_row: "UpstreamProviderRow"
    ) -> "AnthropicUpstreamProvider":
        try:
            settings = json.loads(provider_row.provider_settings or "{}")
        except (TypeError, ValueError):
            settings = {}
        workspace_id = (
            settings.get("workspace_id") if isinstance(settings, dict) else None
        )
        return cls(
            api_key=provider_row.api_key,
            provider_fee=provider_row.provider_fee,
            workspace_id=workspace_id if isinstance(workspace_id, str) else None,
        )

    @classmethod
    def get_provider_metadata(cls) -> dict[str, object]:
        return {
            "id": cls.provider_type,
            "name": "Anthropic",
            "default_base_url": cls.default_base_url,
            "fixed_base_url": True,
            "platform_url": cls.platform_url,
        }

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

    def prepare_headers(self, request_headers: dict) -> dict:
        headers = super().prepare_headers(request_headers)
        # Match Anthropic's SDK default API version while preserving client overrides.
        if not any(name.lower() == "anthropic-version" for name in headers):
            headers["anthropic-version"] = "2023-06-01"
        if self.workspace_id:
            # The configured upstream workspace takes precedence over client headers.
            headers = {
                name: value
                for name, value in headers.items()
                if name.lower() != "anthropic-workspace-id"
            }
            headers["anthropic-workspace-id"] = self.workspace_id
        return headers

    async def fetch_models(self) -> list[Model]:
        """Fetch Anthropic models from OpenRouter API filtered by anthropic source."""
        models_data = await async_fetch_openrouter_models(source_filter="anthropic")
        models = [Model(**model) for model in models_data]  # type: ignore
        for model in models:
            model.alias_ids = [self.transform_model_name(model.id)]
        return models
