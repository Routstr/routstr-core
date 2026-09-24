from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from ..core.logging import get_logger
from ..payment.models import Architecture, Model, Pricing, TopProvider
from .base import BaseUpstreamProvider

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow

logger = get_logger(__name__)

# ``GET /models`` defaults to ``type=text``, which is why a Venice account
# configured as a generic upstream never sees the rest of its catalog.
_MODELS_TYPE_PARAM = "all"

# Families this proxy can both route and price. Image, audio, music and video
# are billed per clip or per second and return no usage object to settle
# against, so exposing them would hand out unpriced inference.
_SUPPORTED_TYPES = frozenset({"text", "embedding"})

# Venice prices text in USD per million tokens; Routstr prices per token.
_USD_PER_MILLION = 1_000_000.0

_ARCHITECTURES: dict[str, tuple[str, list[str], list[str]]] = {
    "text": ("text->text", ["text"], ["text"]),
    "embedding": ("text->embedding", ["text"], ["embedding"]),
}


def _usd(entry: Any) -> float | None:
    """Read the USD leg of a Venice ``{usd, diem}`` price pair."""
    if isinstance(entry, dict):
        value = entry.get("usd")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


class VeniceUpstreamProvider(BaseUpstreamProvider):
    """Upstream provider for the Venice.ai API.

    Venice publishes a complete price book on its own catalog, so models are
    built from that rather than matched against OpenRouter, which has never
    heard of most of Venice's catalog.
    """

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

        pricing = self._parse_pricing(spec.get("pricing"))
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

    def _parse_pricing(self, raw: Any) -> Pricing | None:
        if not isinstance(raw, dict):
            return None

        # The ``extended`` tier some models charge past a context threshold is
        # ignored: billing it would overcharge every request staying under it.
        input_usd = _usd(raw.get("input"))
        if input_usd is None:
            return None
        return Pricing(
            prompt=input_usd / _USD_PER_MILLION,
            completion=(_usd(raw.get("output")) or 0.0) / _USD_PER_MILLION,
            input_cache_read=(_usd(raw.get("cache_input")) or 0.0) / _USD_PER_MILLION,
            input_cache_write=(_usd(raw.get("cache_write")) or 0.0) / _USD_PER_MILLION,
        )
