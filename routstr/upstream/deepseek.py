"""First-class upstream for the DeepSeek API.

Pricing comes from ``_PEAK_RATES`` below, not from litellm or OpenRouter:
litellm's bundled ``deepseek-v4-flash`` entry is stale (input, output and cache
rates alike), the OpenRouter feed carries resale prices below DeepSeek's own
peak rate, and neither the bundled map nor OpenRouter knows the current
``deepseek-flash`` id. A model DeepSeek lists that the table does not
cover is imported disabled rather than priced from those sources.

DeepSeek bills peak hours at twice the off-peak rate. The node has one flat
price per model, so the table holds the PEAK rates: a client may overpay
off-peak but the node never bills below its own cost.

Rates: https://api-docs.deepseek.com/quick_start/pricing (checked 2026-09-30).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import BaseUpstreamProvider
from .generic import GenericUpstreamProvider
from .pricing_resolver import ResolvedPricing

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow

_CONTEXT_LENGTH = 1_000_000
_MAX_OUTPUT_TOKENS = 384_000

# USD per 1M tokens at DeepSeek's peak rate: (input cache miss, output, input
# cache hit). DeepSeek has no cache-write charge.
_FLASH = (0.30, 1.20, 0.006)
_PRO = (1.32, 3.96, 0.044)

_PEAK_RATES: dict[str, tuple[float, float, float]] = {
    "deepseek-flash": _FLASH,
    # Retired ids DeepSeek still accepts, served and billed as deepseek-flash.
    "deepseek-v4-flash": _FLASH,
    "deepseek-v4-flash-vision-exp": _FLASH,
    "deepseek-v4-pro": _PRO,
}

# Pro is the only current model without vision support.
_TEXT_ONLY = {"deepseek-v4-pro"}


class DeepSeekUpstreamProvider(GenericUpstreamProvider):
    """Upstream provider specifically configured for the DeepSeek API."""

    provider_type = "deepseek"
    default_base_url = "https://api.deepseek.com"
    platform_url = "https://platform.deepseek.com/api_keys"
    litellm_provider_prefix = "deepseek/"
    use_fallback_pricing = False

    def __init__(self, api_key: str, provider_fee: float = 1.01):
        super().__init__(
            base_url=self.default_base_url,
            api_key=api_key,
            provider_fee=provider_fee,
            upstream_name="DeepSeek",
        )

    @classmethod
    def _build_from_row(
        cls, provider_row: "UpstreamProviderRow"
    ) -> "DeepSeekUpstreamProvider":
        return cls(api_key=provider_row.api_key, provider_fee=provider_row.provider_fee)

    @classmethod
    def get_provider_metadata(cls) -> dict[str, object]:
        return {
            "id": cls.provider_type,
            "name": "DeepSeek",
            "default_base_url": cls.default_base_url,
            "fixed_base_url": True,
            "platform_url": cls.platform_url,
        }

    def _apply_provider_field(self, response_json: object) -> None:
        # A first-party upstream: stamp "deepseek", not Generic's hostname.
        BaseUpstreamProvider._apply_provider_field(self, response_json)

    def transform_model_name(self, model_id: str) -> str:
        """Strip the 'deepseek/' prefix for DeepSeek API compatibility."""
        return model_id.removeprefix("deepseek/")

    def _native_pricing(
        self, model_id: str, model_spec: dict
    ) -> ResolvedPricing | None:
        """Price ``model_id`` from the peak-rate table; ``None`` if absent."""
        rates = _PEAK_RATES.get(model_id)
        if rates is None:
            return None
        input_usd, output_usd, cache_hit_usd = rates
        input_modalities = ["text"] if model_id in _TEXT_ONLY else ["text", "image"]
        return ResolvedPricing(
            prompt=input_usd / 1_000_000,
            completion=output_usd / 1_000_000,
            context_length=_CONTEXT_LENGTH,
            source="native",
            max_completion_tokens=_MAX_OUTPUT_TOKENS,
            input_cache_read=cache_hit_usd / 1_000_000,
            input_modalities=input_modalities,
        )
