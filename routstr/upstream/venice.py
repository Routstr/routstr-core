from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx

from ..core.logging import get_logger
from ..payment.image_pricing import ImagePriceTier, ImagePricing, produces_images
from ..payment.models import Architecture, Model, Pricing, TopProvider
from .base import BaseUpstreamProvider

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow

logger = get_logger(__name__)

# ``GET /models`` defaults to ``type=text``, which is why a Venice account
# configured as a generic upstream never sees its image catalog.
_MODELS_TYPE_PARAM = "all"

# Families this proxy can both route and price. Audio, music and video are
# billed per second or per clip and return no usage object to settle against,
# so exposing them would hand out unpriced inference.
_SUPPORTED_TYPES = frozenset({"text", "image", "inpaint", "upscale", "embedding"})

_IMAGE_TYPES = frozenset({"image", "inpaint", "upscale"})

# Venice prices text in USD per million tokens; Routstr prices per token.
_USD_PER_MILLION = 1_000_000.0

_ARCHITECTURES: dict[str, tuple[str, list[str], list[str]]] = {
    "text": ("text->text", ["text"], ["text"]),
    "image": ("text->image", ["text"], ["image"]),
    "inpaint": ("text+image->image", ["text", "image"], ["image"]),
    "upscale": ("image->image", ["image"], ["image"]),
    "embedding": ("text->embedding", ["text"], ["embedding"]),
}

# One call may ask for several images, so the ceiling covers a small batch.
_IMAGES_PER_RESERVATION = 4


def _usd(entry: Any) -> float | None:
    """Read the USD leg of a Venice ``{usd, diem}`` price pair."""
    if isinstance(entry, dict):
        value = entry.get("usd")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _max_usd(entry: Any) -> float | None:
    """Worst-case USD price across a nested Venice price table."""
    direct = _usd(entry)
    if direct is not None:
        return direct
    if not isinstance(entry, dict):
        return None
    prices = [p for p in (_max_usd(value) for value in entry.values()) if p is not None]
    return max(prices) if prices else None


def _prices(table: Any, case: Callable[[str], str]) -> dict[str, float]:
    """A Venice ``{label: {usd, diem}}`` table as ``{label: usd}``."""
    if not isinstance(table, dict):
        return {}
    priced = ((case(str(label)), _usd(entry)) for label, entry in table.items())
    return {label: usd for label, usd in priced if usd is not None}


def _tables(table: Any) -> dict[str, dict]:
    """The nested sub-tables of a Venice price table, keyed by their label."""
    if not isinstance(table, dict):
        return {}
    return {
        str(label): entry for label, entry in table.items() if isinstance(entry, dict)
    }


def _label(value: Any, case: Callable[[str], str]) -> str | None:
    return case(value) if isinstance(value, str) else None


def _labels(values: Any, case: Callable[[str], str]) -> list[str]:
    if not isinstance(values, list):
        return []
    return [case(str(value)) for value in values]


class VeniceUpstreamProvider(BaseUpstreamProvider):
    """Upstream provider for the Venice.ai API.

    Venice publishes a complete price book on its own catalog, so models are
    built from that rather than matched against OpenRouter, which has never
    heard of most of Venice's image catalog.
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

        pricing = self._parse_pricing(str(model_type), spec.get("pricing"))
        if pricing is None:
            return None
        image_pricing = (
            self._build_image_pricing(spec.get("pricing"), spec)
            if model_type in _IMAGE_TYPES
            else None
        )

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
            image_pricing=image_pricing,
            top_provider=TopProvider(
                context_length=int(context_length) if context_length else None,
                max_completion_tokens=int(max_completion_tokens)
                if max_completion_tokens
                else None,
            ),
        )

    def _parse_pricing(self, model_type: str, raw: Any) -> Pricing | None:
        if not isinstance(raw, dict):
            return None

        if model_type in _IMAGE_TYPES:
            per_image = self._per_image_usd(raw)
            if per_image is None:
                return None
            return Pricing(prompt=0.0, completion=0.0, image=per_image)

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

    def _build_image_pricing(
        self, raw: Any, spec: dict[str, Any]
    ) -> ImagePricing | None:
        """Venice's per-tier image prices as the model's own price book.

        ``constraints`` carries the resolution and quality applied when the
        request names neither, so a default request is priced at the default
        tier rather than the ceiling.
        """
        if not isinstance(raw, dict):
            return None
        max_usd = self._per_image_usd(raw)
        if max_usd is None:
            return None

        tiers = [
            ImagePriceTier(resolution=label, usd=price)
            for label, price in _prices(raw.get("resolutions"), str.upper).items()
        ]
        for label, steps in _tables(raw.get("quality")).items():
            tiers += [
                ImagePriceTier(resolution=label.upper(), quality=step, usd=price)
                for step, price in _prices(steps, str.lower).items()
            ]

        constraints = spec.get("constraints")
        constraints = constraints if isinstance(constraints, dict) else {}

        return ImagePricing(
            max_usd=max_usd,
            tiers=tiers,
            default_resolution=_label(constraints.get("defaultResolution"), str.upper),
            default_quality=_label(constraints.get("defaultQuality"), str.lower),
            resolutions=_labels(constraints.get("resolutions"), str.upper),
            qualities=_labels(constraints.get("qualities"), str.lower),
            upscale=_prices(raw.get("upscale"), str.lower),
        )

    @staticmethod
    def _per_image_usd(raw: dict[str, Any]) -> float | None:
        """Worst-case USD for one generation.

        ``upscale`` and ``inputImages`` price a separate call and a per-extra-
        image surcharge, so folding them in would inflate every reservation.
        """
        candidates = [
            _usd(raw.get("generation")),
            _usd(raw.get("inpaint")),
            _max_usd(raw.get("resolutions")),
            _max_usd(raw.get("quality")),
        ]
        priced = [c for c in candidates if c is not None]
        return max(priced) if priced else None

    def _apply_provider_fee_to_model(self, model: Model) -> Model:
        """Reserve a batch of images for image models, tokens for the rest.

        The inherited max-cost formula reads a per-image rate as a per-input-
        image surcharge, reserving a hundred generations for one image.
        """
        if not produces_images(model):
            return super()._apply_provider_fee_to_model(model)

        adjusted = Pricing.parse_obj(
            {k: v * self.provider_fee for k, v in model.pricing.dict().items()}
        )
        adjusted.max_prompt_cost = 0.0
        adjusted.max_completion_cost = adjusted.image * _IMAGES_PER_RESERVATION
        adjusted.max_cost = adjusted.max_completion_cost
        return model.copy(update={"pricing": adjusted})
