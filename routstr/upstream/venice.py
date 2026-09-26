from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from ..core.exceptions import UpstreamError
from ..core.logging import get_logger
from ..payment.models import Architecture, Model, Pricing, TopProvider
from .base import BaseUpstreamProvider

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow

logger = get_logger(__name__)

# Venice's ``/models`` returns only text models unless asked for all.
_MODELS_TYPE_PARAM = "all"

# Other families bill per clip or second and return no usage to settle.
_SUPPORTED_TYPES = frozenset({"text", "embedding"})

_USD_PER_MILLION = 1_000_000.0

_ARCHITECTURES: dict[str, tuple[str, list[str], list[str]]] = {
    "text": ("text->text", ["text"], ["text"]),
    "embedding": ("text->embedding", ["text"], ["embedding"]),
}

# ``auto`` leaves the search decision to the model, as Anthropic does.
# Citations are the caller's only sign of a search: litellm drops
# ``venice_parameters`` from the response, leaving the inline ``^n^`` markers.
_WEB_SEARCH_SUFFIX = ":enable_web_search=auto&enable_web_citations=true"

# Refused rather than silently ignored, since Venice cannot enforce them.
_UNENFORCEABLE_WEB_SEARCH_KEYS = frozenset(
    {"allowed_domains", "blocked_domains", "user_location"}
)


def _is_web_search_tool(tool: Any) -> bool:
    """Mirror litellm's detection, so every tool it would rewrite is caught."""
    if not isinstance(tool, dict):
        return False
    tool_type = tool.get("type")
    return (
        isinstance(tool_type, str) and tool_type.startswith("web_search")
    ) or tool.get("name") == "web_search"


def _usd(entry: Any) -> float | None:
    """Read the USD leg of a Venice ``{usd, diem}`` price pair."""
    if isinstance(entry, dict):
        value = entry.get("usd")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _is_unenforceable(key: str, value: Any) -> bool:
    if key in _UNENFORCEABLE_WEB_SEARCH_KEYS:
        return value is not None and value != []
    if key == "max_uses":
        # ``auto`` searches at most once, so only an integer cap of 1+ is met.
        is_count = isinstance(value, int) and not isinstance(value, bool)
        return value is not None and not (is_count and value >= 1)
    return False


class VeniceUpstreamProvider(BaseUpstreamProvider):
    """Venice.ai upstream, priced from Venice's own catalog since OpenRouter
    lists little of it."""

    provider_type = "venice"
    default_base_url = "https://api.venice.ai/api/v1"
    platform_url = "https://venice.ai/settings/api"

    def __init__(self, api_key: str, provider_fee: float = 1.01):
        super().__init__(
            base_url=self.default_base_url, api_key=api_key, provider_fee=provider_fee
        )

    @classmethod
    def _build_from_row(
        cls, provider_row: "UpstreamProviderRow"
    ) -> "VeniceUpstreamProvider":
        return cls(
            api_key=provider_row.api_key,
            provider_fee=provider_row.provider_fee,
        )

    @classmethod
    def get_provider_metadata(cls) -> dict[str, object]:
        return {
            "id": cls.provider_type,
            "name": "Venice AI",
            "default_base_url": cls.default_base_url,
            "fixed_base_url": True,
            "platform_url": cls.platform_url,
        }

    def transform_model_name(self, model_id: str) -> str:
        return model_id.removeprefix("venice/")

    def adapt_messages_request(self, body: dict, model_obj: Model) -> str:
        """Swap an Anthropic web-search tool for Venice's model-name suffix.

        litellm would turn the tool into ``web_search_options``, which Venice
        rejects with a 400.
        """
        tools = body.get("tools")
        if not isinstance(tools, list):
            return ""
        search_tools = [tool for tool in tools if _is_web_search_tool(tool)]
        if not search_tools:
            return ""

        unenforceable = sorted(
            {
                key
                for tool in search_tools
                for key, value in tool.items()
                if _is_unenforceable(key, value)
            }
        )
        if unenforceable:
            raise UpstreamError(
                "Venice web search cannot honour these Anthropic web_search "
                f"options: {', '.join(unenforceable)}",
                status_code=400,
                code="UNSUPPORTED_WEB_SEARCH_OPTION",
                details={"unsupported_options": unenforceable},
            )

        tool_choice = body.get("tool_choice")
        if isinstance(tool_choice, dict) and tool_choice.get("name") == "web_search":
            raise UpstreamError(
                "Venice web search cannot be forced through tool_choice; it is "
                "decided by the model",
                status_code=400,
                code="UNSUPPORTED_WEB_SEARCH_OPTION",
                details={"unsupported_options": ["tool_choice"]},
            )

        remaining = [tool for tool in tools if not _is_web_search_tool(tool)]
        if remaining:
            # ``tool_choice: any`` now requires a function tool; kept as is,
            # like OpenRouter, rather than guessing the caller's intent.
            body["tools"] = remaining
        else:
            body.pop("tools", None)
            # OpenAI-shaped upstreams reject tool_choice without tools.
            body.pop("tool_choice", None)

        return _WEB_SEARCH_SUFFIX

    async def _fetch_provider_models(self) -> dict:
        url = f"{self.base_url.rstrip('/')}/models"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else None
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                url, params={"type": _MODELS_TYPE_PARAM}, headers=headers
            )
            response.raise_for_status()
            return response.json()

    async def fetch_models(self) -> list[Model]:
        try:
            payload = await self._fetch_provider_models()
        except Exception as e:
            logger.error(
                "Error fetching Venice models",
                extra={"error": str(e), "error_type": type(e).__name__},
            )
            return []

        models: list[Model] = []
        skipped: list[str] = []
        for entry in payload.get("data", []):
            if not isinstance(entry, dict):
                continue
            try:
                model = self._parse_model(entry)
            except Exception as e:
                logger.warning(
                    "Failed to parse Venice model",
                    extra={
                        "model_id": entry.get("id", "unknown"),
                        "error": str(e),
                        "error_type": type(e).__name__,
                    },
                )
                continue
            if model is None:
                skipped.append(str(entry.get("id", "unknown")))
                continue
            models.append(model)

        if skipped:
            logger.debug(
                f"({len(skipped)}) Venice models skipped as unsupported or unpriced",
                extra={"skipped_models": skipped},
            )
        return models

    def _parse_model(self, entry: dict[str, Any]) -> Model | None:
        model_type = entry.get("type")
        model_id = entry.get("id")
        spec = entry.get("model_spec")
        if not model_id or model_type not in _SUPPORTED_TYPES:
            return None
        if not isinstance(spec, dict) or spec.get("offline"):
            return None

        pricing = self._parse_pricing(spec.get("pricing"), str(model_type))
        if pricing is None:
            return None

        modality, input_modalities, output_modalities = _ARCHITECTURES[str(model_type)]
        capabilities = spec.get("capabilities")
        if (
            model_type == "text"
            and isinstance(capabilities, dict)
            and capabilities.get("supportsVision")
        ):
            input_modalities = [*input_modalities, "image"]
            modality = "text+image->text"

        context_length = spec.get("availableContextTokens")
        max_completion_tokens = spec.get("maxCompletionTokens")
        name = spec.get("name") or str(model_id)

        return Model(
            id=str(model_id),
            name=str(name),
            created=int(entry.get("created") or 0),
            description=str(spec.get("description") or f"Venice {model_type} model"),
            context_length=int(context_length) if context_length else 0,
            architecture=Architecture(
                modality=modality,
                input_modalities=input_modalities,
                output_modalities=output_modalities,
                tokenizer="Unknown",
                instruct_type=None,
            ),
            pricing=pricing,
            top_provider=TopProvider(
                context_length=int(context_length) if context_length else None,
                max_completion_tokens=int(max_completion_tokens)
                if max_completion_tokens
                else None,
            ),
        )

    def _parse_pricing(self, raw: Any, model_type: str) -> Pricing | None:
        if not isinstance(raw, dict):
            return None

        # The long-context ``extended`` tier is ignored; billing it would
        # overcharge every shorter request.
        input_usd = _usd(raw.get("input"))
        output_usd = _usd(raw.get("output"))
        # Only embeddings may omit an output price. Free or negative prices
        # are dropped, as in ``generic.py``.
        if output_usd is None and model_type == "embedding":
            output_usd = 0.0
        if input_usd is None or output_usd is None:
            return None
        if input_usd < 0 or output_usd < 0 or (input_usd == 0 and output_usd == 0):
            return None
        return Pricing(
            prompt=input_usd / _USD_PER_MILLION,
            completion=output_usd / _USD_PER_MILLION,
            input_cache_read=(_usd(raw.get("cache_input")) or 0.0) / _USD_PER_MILLION,
            input_cache_write=(_usd(raw.get("cache_write")) or 0.0) / _USD_PER_MILLION,
        )
