import json
from typing import TYPE_CHECKING

import httpx

from ..core.logging import get_logger
from ..payment.image_pricing import produces_images
from ..payment.models import Model, async_fetch_openrouter_models
from .base import BaseUpstreamProvider, _reported_provider
from .image_catalog import (
    attach_image_books,
    fetch_openrouter_image_books,
    openrouter_book_from_pricing,
)
from .model_paths import public_provider_url

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow

logger = get_logger(__name__)

_UNKNOWN_SUB_PROVIDER = "unknown"


def _carries_usage(payload: dict) -> bool:
    """Whether a payload holds usage, at top level or in the Anthropic
    ``message`` / Responses ``response`` envelope."""
    return any(
        isinstance(obj, dict) and isinstance(obj.get("usage"), dict)
        for obj in (payload, payload.get("message"), payload.get("response"))
    )


class OpenRouterUpstreamProvider(BaseUpstreamProvider):
    """Upstream provider specifically configured for OpenRouter API."""

    provider_type = "openrouter"
    default_base_url = "https://openrouter.ai/api/v1"
    platform_url = "https://openrouter.ai/settings/keys"
    supports_anthropic_messages = True
    litellm_provider_prefix = "openrouter/"

    def _apply_provider_field(self, response_json: object) -> None:
        """Stamp the ``provider`` field for OpenRouter responses.

        OpenRouter is a router, not the real serving provider, so a bare
        ``"openrouter"`` value carries no useful information. Rules:

        - Real upstream sub-provider (e.g. ``"GMICloud"``) -> ``"openrouter:GMICloud"``.
        - Missing sub-provider, or one that merely echoes ``"openrouter"`` ->
          ``"openrouter:unknown"``: the router is still known even when the
          serving provider is not (e.g. the Responses API never reports it).
        - Idempotent: re-stamping never produces ``"openrouter:openrouter:..."``;
          the ``openrouter:`` prefix appears at most once.
        """
        if not isinstance(response_json, dict):
            return
        response_json["provider_url"] = public_provider_url(self.base_url)
        provider_type = (self.provider_type or "").strip()
        sub = _reported_provider(response_json) or ""
        # Strip any already-applied "openrouter:" prefixes (idempotency).
        prefix = f"{provider_type}:"
        while sub.lower().startswith(prefix.lower()):
            sub = sub[len(prefix) :].strip()
        # Already stamped as unknown on an earlier pass; keep it without
        # warning again.
        if sub.lower() == _UNKNOWN_SUB_PROVIDER:
            response_json["provider"] = f"{provider_type}:{_UNKNOWN_SUB_PROVIDER}"
            return
        # No real sub-provider, or it just echoes our own router name.
        if not sub or sub.lower() == provider_type.lower():
            # Warn only on the billed payload, not on every stream chunk.
            if _carries_usage(response_json):
                logger.warning(
                    "OpenRouter did not report the serving provider",
                    extra={
                        "model": response_json.get("model"),
                        "response_id": response_json.get("id"),
                    },
                )
            response_json["provider"] = f"{provider_type}:{_UNKNOWN_SUB_PROVIDER}"
            return
        response_json["provider"] = f"{provider_type}:{sub}"

    def __init__(self, api_key: str, provider_fee: float = 1.06):
        """Initialize OpenRouter provider with API key.

        Args:
            api_key: OpenRouter API key for authentication
            provider_fee: Provider fee multiplier (default 1.06 for 6% fee)
        """
        super().__init__(
            base_url=self.default_base_url, api_key=api_key, provider_fee=provider_fee
        )

    def prepare_request_body(
        self,
        body: bytes | None,
        model_obj: Model,
        include_stream_usage: bool = False,
    ) -> bytes | None:
        body = super().prepare_request_body(body, model_obj, include_stream_usage)
        if not body or not produces_images(model_obj):
            return body
        # OpenRouter would otherwise re-route a failed generation to another
        # provider on its side, which can buy twice for one settled response.
        try:
            data = json.loads(body)
        except Exception:
            return body
        if not isinstance(data, dict):
            return body
        routing = data.get("provider")
        routing = dict(routing) if isinstance(routing, dict) else {}
        if routing.get("allow_fallbacks") is False:
            return body
        routing["allow_fallbacks"] = False
        data["provider"] = routing
        return json.dumps(data).encode()

    @classmethod
    def _build_from_row(
        cls, provider_row: "UpstreamProviderRow"
    ) -> "OpenRouterUpstreamProvider":
        return cls(
            api_key=provider_row.api_key,
            provider_fee=provider_row.provider_fee,
        )

    @classmethod
    def get_provider_metadata(cls) -> dict[str, object]:
        return {
            "id": cls.provider_type,
            "name": "OpenRouter",
            "default_base_url": cls.default_base_url,
            "fixed_base_url": True,
            "platform_url": cls.platform_url,
            "can_show_balance": True,
        }

    async def fetch_models(self) -> list[Model]:
        """Fetch all OpenRouter models.

        Image models are priced from the Image API's per-endpoint billable
        lines, which name the unit (image, megapixel or token) and any
        resolution variants. When that listing is unavailable the catalog's
        ``image_output`` token rate stands in. Either way the response's
        ``usage.cost`` settles the charge.
        """
        models_data = await async_fetch_openrouter_models()
        models = [Model(**model) for model in models_data]  # type: ignore
        # manual alias for openai/text-embedding-ada-002 due to openrouter api bug
        for model in models:
            if model.id == "openai/text-embedding-ada-002":
                model.alias_ids = ["text-embedding-ada-002-v2"]
                break

        image_ids = [
            m.id for m in models if m.architecture.output_modalities == ["image"]
        ]
        books = await fetch_openrouter_image_books(
            image_ids, base_url=self.base_url, api_key=self.api_key
        )
        for entry in models_data:
            model_id = str(entry.get("id", ""))
            pricing = entry.get("pricing")
            if model_id in books or model_id not in image_ids:
                continue
            fallback = (
                openrouter_book_from_pricing(pricing, model_id)
                if isinstance(pricing, dict)
                else None
            )
            if fallback is not None:
                books[model_id] = fallback
        return attach_image_books(models, books, source="OpenRouter")

    async def get_balance(self) -> float | None:
        """Get the current account balance from OpenRouter.

        Returns:
            Float representing the balance amount (in credits/USD), or None if unavailable.
        """
        url = f"{self.base_url}/credits"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                data = response.json()

                credits_data = data.get("data", {})
                total_credits = float(credits_data.get("total_credits", 0.0))
                total_usage = float(credits_data.get("total_usage", 0.0))

                return total_credits - total_usage
        except Exception:
            return None
