import json
from typing import TYPE_CHECKING

from litellm.llms.openai.chat.gpt_5_transformation import OpenAIGPT5Config
from litellm.llms.openai.chat.o_series_transformation import OpenAIOSeriesConfig

from ..payment.models import Model, async_fetch_openrouter_models
from .base import BaseUpstreamProvider

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow

_O_SERIES = OpenAIOSeriesConfig()


def _rejects_max_tokens(model: str) -> bool:
    return OpenAIGPT5Config.is_model_gpt_5_model(
        model
    ) or _O_SERIES.is_model_o_series_model(model)


class OpenAIUpstreamProvider(BaseUpstreamProvider):
    """Upstream provider specifically configured for OpenAI API."""

    provider_type = "openai"
    default_base_url = "https://api.openai.com/v1"
    platform_url = "https://platform.openai.com/api-keys"

    def __init__(self, api_key: str, provider_fee: float = 1.01):
        super().__init__(
            base_url=self.default_base_url, api_key=api_key, provider_fee=provider_fee
        )

    @classmethod
    def _build_from_row(
        cls, provider_row: "UpstreamProviderRow"
    ) -> "OpenAIUpstreamProvider":
        return cls(
            api_key=provider_row.api_key,
            provider_fee=provider_row.provider_fee,
        )

    @classmethod
    def get_provider_metadata(cls) -> dict[str, object]:
        return {
            "id": cls.provider_type,
            "name": "OpenAI",
            "default_base_url": cls.default_base_url,
            "fixed_base_url": True,
            "platform_url": cls.platform_url,
        }

    def transform_model_name(self, model_id: str) -> str:
        """Strip 'openai/' prefix for OpenAI API compatibility."""
        return model_id.removeprefix("openai/")

    def prepare_request_body(
        self,
        body: bytes | None,
        model_obj: Model,
        include_stream_usage: bool = False,
    ) -> bytes | None:
        body = super().prepare_request_body(body, model_obj, include_stream_usage)
        if not body:
            return body
        try:
            data = json.loads(body)
        except ValueError:
            return body
        # Reasoning models 400 on max_tokens; renaming up front saves the
        # reject-and-retry round trip. Names litellm doesn't know yet still
        # fall through to request_correction's reactive rename.
        if (
            isinstance(data, dict)
            and "messages" in data
            and "max_tokens" in data
            and "max_completion_tokens" not in data
            and _rejects_max_tokens(self.transform_model_name(model_obj.id))
        ):
            data["max_completion_tokens"] = data.pop("max_tokens")
            return json.dumps(data).encode()
        return body

    async def fetch_models(self) -> list[Model]:
        """Fetch OpenAI models from OpenRouter API filtered by openai source."""
        models_data = await async_fetch_openrouter_models(source_filter="openai")
        return [Model(**model) for model in models_data]  # type: ignore
