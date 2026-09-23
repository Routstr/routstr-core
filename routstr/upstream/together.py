"""Upstream provider for the Together AI API.

Together is OpenAI-compatible for chat and images. Its ``/models`` listing
prices text per million tokens but says nothing about images, which are
priced per image or per megapixel on its pricing page only. The image book
therefore comes from a table of published prices, which the operator can
extend or correct per model through ``provider_settings.image_prices``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx

from ..core.logging import get_logger
from ..payment.image_pricing import ImagePricing
from ..payment.models import Architecture, Model, Pricing, TopProvider
from .base import BaseUpstreamProvider
from .image_catalog import attach_image_books, static_image_book

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow

logger = get_logger(__name__)

_USD_PER_MILLION = 1_000_000.0

_TEXT_TYPES = frozenset({"chat", "language", "code"})
_IMAGE_TYPES = frozenset({"image"})
_EMBEDDING_TYPES = frozenset({"embedding"})

# Published Together image prices, keyed by lower-cased model id, as
# ``(usd, unit)`` with unit ``image`` or ``megapixel``. Checked against the
# pricing page; a model missing here is not listed until an operator prices
# it in ``provider_settings``.
TOGETHER_IMAGE_USD: dict[str, tuple[float, str]] = {
    "black-forest-labs/flux.1-schnell": (0.0027, "megapixel"),
    "black-forest-labs/flux.1-dev": (0.025, "megapixel"),
    "black-forest-labs/flux.1-krea-dev": (0.025, "megapixel"),
    "black-forest-labs/flux.1-pro": (0.05, "megapixel"),
    "black-forest-labs/flux.1.1-pro": (0.04, "megapixel"),
    "black-forest-labs/flux.1-kontext-dev": (0.025, "megapixel"),
    "black-forest-labs/flux.1-kontext-pro": (0.04, "image"),
    "black-forest-labs/flux.1-kontext-max": (0.08, "image"),
    "black-forest-labs/flux.2-pro": (0.03, "megapixel"),
    "black-forest-labs/flux.2-dev": (0.025, "megapixel"),
    "black-forest-labs/flux.2-flex": (0.06, "megapixel"),
    "black-forest-labs/flux.2-max": (0.07, "megapixel"),
    "bytedance-seed/seedream-3.0": (0.018, "image"),
    "bytedance-seed/seedream-4.0": (0.03, "image"),
    "google/imagen-4.0-generate": (0.04, "image"),
    "google/imagen-4.0-ultra": (0.06, "image"),
    "google/nano-banana": (0.039, "image"),
}

_IMAGE_PRICE_SETTING = "image_prices"


def _usd_per_million(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) / _USD_PER_MILLION
    return None


def _override_book(entry: Any) -> ImagePricing | None:
    """A book from one ``provider_settings.image_prices`` entry.

    Accepts a bare USD number (per image) or ``{"usd": .., "unit": ..}``.
    """
    if isinstance(entry, bool):
        return None
    if isinstance(entry, (int, float)):
        return static_image_book(float(entry))
    if isinstance(entry, dict):
        usd = entry.get("usd")
        unit = entry.get("unit", "image")
        if isinstance(usd, (int, float)) and not isinstance(usd, bool):
            return static_image_book(float(usd), str(unit))
    return None


class TogetherUpstreamProvider(BaseUpstreamProvider):
    """Upstream provider specifically configured for the Together AI API."""

    provider_type = "together"
    default_base_url = "https://api.together.xyz/v1"
    platform_url = "https://api.together.ai/settings/api-keys"
    litellm_provider_prefix = "together_ai/"

    def __init__(
        self,
        api_key: str,
        provider_fee: float = 1.01,
        image_prices: dict[str, Any] | None = None,
    ):
        super().__init__(
            base_url=self.default_base_url, api_key=api_key, provider_fee=provider_fee
        )
        self.image_prices = {str(k).lower(): v for k, v in (image_prices or {}).items()}

    @classmethod
    def _build_from_row(
        cls, provider_row: "UpstreamProviderRow"
    ) -> "TogetherUpstreamProvider":
        settings: dict[str, Any] = {}
        raw = getattr(provider_row, "provider_settings", None)
        if raw:
            try:
                parsed = json.loads(raw)
                settings = parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                settings = {}
        image_prices = settings.get(_IMAGE_PRICE_SETTING)
        return cls(
            api_key=provider_row.api_key,
            provider_fee=provider_row.provider_fee,
            image_prices=image_prices if isinstance(image_prices, dict) else None,
        )

    @classmethod
    def get_provider_metadata(cls) -> dict[str, object]:
        return {
            "id": cls.provider_type,
            "name": "Together AI",
            "default_base_url": cls.default_base_url,
            "fixed_base_url": True,
            "platform_url": cls.platform_url,
        }

    def transform_model_name(self, model_id: str) -> str:
        return model_id.removeprefix("together/")

    def image_book(self, model_id: str) -> ImagePricing | None:
        """The operator's price for ``model_id`` if set, else the published one."""
        key = model_id.lower()
        override = _override_book(self.image_prices.get(key))
        if override is not None:
            return override
        published = TOGETHER_IMAGE_USD.get(key)
        if published is None:
            return None
        usd, unit = published
        return static_image_book(usd, unit)

    async def _fetch_catalog(self) -> list[dict[str, Any]]:
        url = f"{self.base_url.rstrip('/')}/models"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else None
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
        # Together answers with a bare list; tolerate the OpenAI ``data`` wrapper.
        if isinstance(payload, dict):
            payload = payload.get("data", [])
        return (
            [entry for entry in payload if isinstance(entry, dict)]
            if isinstance(payload, list)
            else []
        )

    async def fetch_models(self) -> list[Model]:
        try:
            entries = await self._fetch_catalog()
        except Exception as e:
            logger.error(
                "Error fetching Together models",
                extra={"error": str(e), "error_type": type(e).__name__},
            )
            return []

        models: list[Model] = []
        books: dict[str, ImagePricing] = {}
        skipped: list[str] = []
        for entry in entries:
            try:
                model = self._parse_model(entry)
            except Exception as e:
                logger.warning(
                    "Failed to parse Together model",
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
            if model.architecture.output_modalities == ["image"]:
                book = self.image_book(model.id)
                if book is not None:
                    books[model.id] = book

        if skipped:
            logger.debug(
                f"({len(skipped)}) Together models skipped as unsupported or unpriced",
                extra={"skipped_models": skipped},
            )
        return attach_image_books(models, books, source="Together")

    def _parse_model(self, entry: dict[str, Any]) -> Model | None:
        model_id = entry.get("id")
        model_type = str(entry.get("type") or "").lower()
        if not model_id:
            return None
        pricing_raw = entry.get("pricing")
        pricing_raw = pricing_raw if isinstance(pricing_raw, dict) else {}

        if model_type in _IMAGE_TYPES:
            modality = ("text->image", ["text"], ["image"])
            # The ceiling is filled in from the book by ``attach_image_books``.
            pricing = Pricing(prompt=0.0, completion=0.0, image=0.0)
        elif model_type in _EMBEDDING_TYPES:
            prompt = _usd_per_million(pricing_raw.get("input"))
            if prompt is None:
                return None
            modality = ("text->embedding", ["text"], ["embedding"])
            pricing = Pricing(prompt=prompt, completion=0.0)
        elif model_type in _TEXT_TYPES:
            prompt = _usd_per_million(pricing_raw.get("input"))
            completion = _usd_per_million(pricing_raw.get("output"))
            if prompt is None or completion is None:
                return None
            modality = ("text->text", ["text"], ["text"])
            pricing = Pricing(prompt=prompt, completion=completion)
        else:
            return None

        context_length = entry.get("context_length")
        context_length = int(context_length) if isinstance(context_length, int) else 0
        name = entry.get("display_name") or str(model_id)
        organization = entry.get("organization")
        description = f"{name} via Together" + (
            f" ({organization})"
            if isinstance(organization, str) and organization
            else ""
        )

        return Model(
            id=str(model_id),
            name=str(name),
            created=int(entry.get("created") or 0),
            description=description,
            context_length=context_length,
            architecture=Architecture(
                modality=modality[0],
                input_modalities=modality[1],
                output_modalities=modality[2],
                tokenizer="Unknown",
                instruct_type=None,
            ),
            pricing=pricing,
            top_provider=TopProvider(
                context_length=context_length or None,
                max_completion_tokens=None,
            ),
        )
