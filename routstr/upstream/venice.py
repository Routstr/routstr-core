from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx

from ..core.exceptions import UpstreamError
from ..core.logging import get_logger
from ..payment.image_pricing import ImagePriceTier, ImagePricing
from ..payment.models import Architecture, Model, Pricing, TopProvider
from .base import BaseUpstreamProvider

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow

logger = get_logger(__name__)

# ``GET /models`` defaults to ``type=text``, which is why a Venice account
# configured as a generic upstream never sees the rest of its catalog.
_MODELS_TYPE_PARAM = "all"

# Families this proxy can both route and price. Audio, music and video are
# billed per clip or per second and return no usage object to settle against,
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

# Venice runs search itself and reports it back through ``venice_parameters``;
# it has no Anthropic-shaped server tool and rejects the ``web_search_options``
# that litellm's Anthropic adapter derives from one. ``auto`` matches Anthropic
# semantics, where declaring the tool leaves the decision to the model.
# Citations are asked for because litellm's Anthropic response translation
# carries no ``venice_parameters``, so the inline ``^n^`` markers Venice writes
# into the text are the only way a caller sees that sources were used.
_WEB_SEARCH_SUFFIX = ":enable_web_search=auto&enable_web_citations=true"

# Anthropic web-search constraints with no Venice equivalent. Honouring the
# request means enforcing them, so a request that sets one is refused rather
# than answered by a search that ignored it. ``max_uses`` is absent on purpose:
# ``auto`` runs at most one search per request, so any cap of 1 or more is
# already met, while domain filters and location would be silently ignored.
# Only ``max_uses: 0``, a request for no search at all, cannot be honoured.
_UNENFORCEABLE_WEB_SEARCH_KEYS = frozenset(
    {"allowed_domains", "blocked_domains", "user_location"}
)


def _is_web_search_tool(tool: Any) -> bool:
    """An Anthropic server-side web-search tool, by either of its markers.

    Matches litellm's own detection (``litellm/llms/anthropic/
    experimental_pass_through/adapters/transformation.py``), so every tool it
    would turn into ``web_search_options`` is caught here first.
    """
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

    def adapt_messages_request(self, body: dict, model_obj: Model) -> str:
        """Trade an Anthropic web-search tool for Venice's own search switch.

        Left in the body, litellm's Anthropic adapter rewrites the tool into a
        top-level ``web_search_options``, which Venice answers with a 400. The
        tool is lifted out here and the same intent re-expressed as a model
        feature suffix, the one form of ``venice_parameters`` that survives
        that adapter.
        """
        tools = body.get("tools")
        if not isinstance(tools, list):
            return ""
        search_tools = [tool for tool in tools if _is_web_search_tool(tool)]
        if not search_tools:
            return ""

        # A key carrying null or an empty list states no constraint, so it is
        # read as absent rather than refused. ``auto`` runs at most one search,
        # so only an integer ``max_uses`` of one or more is known to be met.
        unenforceable = sorted(
            {
                key
                for tool in search_tools
                for key, value in tool.items()
                if (
                    key in _UNENFORCEABLE_WEB_SEARCH_KEYS
                    and value is not None
                    and value != []
                )
                or (
                    key == "max_uses"
                    and value is not None
                    and not (
                        isinstance(value, int)
                        and not isinstance(value, bool)
                        and value >= 1
                    )
                )
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
            # A caller's ``tool_choice: any`` is kept and litellm maps it to
            # OpenAI ``required``, so one of the remaining function tools must
            # now be called where Anthropic would have let a search satisfy it.
            # Deliberate: OpenRouter never rewrites tool_choice for web search
            # either, and guessing an alternative would change caller intent.
            body["tools"] = remaining
        else:
            body.pop("tools", None)
            # tool_choice without tools is rejected by OpenAI-shaped upstreams.
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

    def _parse_pricing(self, raw: Any, model_type: str) -> Pricing | None:
        if not isinstance(raw, dict):
            return None

        if model_type in _IMAGE_TYPES:
            per_image = self._per_image_usd(raw)
            if per_image is None:
                return None
            return Pricing(
                prompt=0.0,
                completion=0.0,
                image=self._input_image_usd(raw),
                image_output=per_image,
            )

        # The ``extended`` tier some models charge past a context threshold is
        # ignored: billing it would overcharge every request staying under it.
        input_usd = _usd(raw.get("input"))
        output_usd = _usd(raw.get("output"))
        # Embeddings produce no completion tokens, so only they may omit an
        # output price. Anywhere else a missing or all-zero price would serve
        # completions free and a negative one would credit the caller, the
        # same guards ``generic.py`` applies to this price book.
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
        input_images = raw.get("inputImages")
        input_images = input_images if isinstance(input_images, dict) else {}
        included = input_images.get("included")

        return ImagePricing(
            max_usd=max_usd,
            tiers=tiers,
            default_resolution=_label(constraints.get("defaultResolution"), str.upper),
            default_quality=_label(constraints.get("defaultQuality"), str.lower),
            resolutions=_labels(constraints.get("resolutions"), str.upper),
            qualities=_labels(constraints.get("qualities"), str.lower),
            upscale=_prices(raw.get("upscale"), str.lower),
            input_image_usd=self._input_image_usd(raw),
            input_images_included=included
            if isinstance(included, int) and not isinstance(included, bool)
            else 0,
        )

    @staticmethod
    def _input_image_usd(raw: dict[str, Any]) -> float:
        """Venice's per-extra-reference-image surcharge, ``inputImages.additional``."""
        input_images = raw.get("inputImages")
        if not isinstance(input_images, dict):
            return 0.0
        return _usd(input_images.get("additional")) or 0.0

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
