from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from .base import BaseUpstreamProvider
from .pricing_resolver import (
    FallbackPricingResolver,
    ResolvedPricing,
    estimate_context_length,
)

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow
    from ..payment.models import Model

from ..core.logging import get_logger

logger = get_logger(__name__)


class GenericUpstreamProvider(BaseUpstreamProvider):
    """Generic upstream provider that can fetch models from any OpenAI-compatible API."""

    provider_type = "generic"
    default_base_url = "http://localhost:8888"
    platform_url = None

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        provider_fee: float = 1.01,
        upstream_name: str | None = None,
    ):
        """Initialize generic provider.

        Args:
            base_url: Base URL of the upstream API endpoint
            api_key: Optional API key for authentication
            provider_fee: Provider fee multiplier (default 1.01 for 1% fee)
            upstream_name: Optional name for the upstream provider
        """
        self.upstream_name = upstream_name or "generic"
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            provider_fee=provider_fee,
        )

    @classmethod
    def _build_from_row(
        cls, provider_row: "UpstreamProviderRow"
    ) -> "GenericUpstreamProvider":
        return cls(
            base_url=provider_row.base_url,
            api_key=provider_row.api_key,
            provider_fee=provider_row.provider_fee,
        )

    @classmethod
    def get_provider_metadata(cls) -> dict[str, object]:
        return {
            "id": cls.provider_type,
            "name": "Generic",
            "default_base_url": cls.default_base_url,
            "fixed_base_url": False,
            "platform_url": cls.platform_url,
        }

    def _native_pricing(
        self, model_id: str, model_spec: dict
    ) -> ResolvedPricing | None:
        """Read pricing/metadata from Venice's bespoke ``model_spec`` schema.

        Returns ``None`` when the upstream reported no native price (the common
        case for bare OpenAI-compatible ``/models`` responses), so the caller
        falls through to the shared resolution chain instead of fabricating a
        number.
        """
        pricing_info = model_spec.get("pricing", {})
        input_usd = pricing_info.get("input", {}).get("usd")
        output_usd = pricing_info.get("output", {}).get("usd")
        if input_usd is None or output_usd is None:
            return None

        capabilities = model_spec.get("capabilities", {})
        input_modalities = ["text"]
        if capabilities.get("supportsVision", False):
            input_modalities.append("image")

        return ResolvedPricing(
            prompt=input_usd / 1_000_000,
            completion=output_usd / 1_000_000,
            context_length=model_spec.get("availableContextTokens"),
            source="native",
            input_modalities=input_modalities,
        )

    async def fetch_models(self) -> list[Model]:
        """Fetch models from upstream API using /models endpoint."""
        from ..payment.models import Architecture, Model, Pricing, TopProvider

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                headers = {}
                if self.api_key:
                    headers["Authorization"] = f"Bearer {self.api_key}"

                response = await client.get(f"{self.base_url}/models", headers=headers)
                response.raise_for_status()
                data = response.json()

                resolver = FallbackPricingResolver()
                models_list = []
                for model_data in data.get("data", []):
                    model_id = model_data.get("id", "")
                    if not model_id:
                        continue

                    model_name = model_data.get("name", model_id)
                    created = model_data.get("created", 0)
                    owned_by = model_data.get("owned_by", "unknown")
                    model_spec = model_data.get("model_spec", {})

                    resolved = self._native_pricing(model_id, model_spec)
                    if resolved is None:
                        resolved = await resolver.resolve(model_id)

                    if resolved is None:
                        # Fail closed: never invent a price. Import the model
                        # disabled with a warning so the operator can price it
                        # (the admin UI surfaces disabled remote models).
                        logger.warning(
                            f"No pricing source resolved for '{model_id}' from "
                            f"{self.upstream_name}; importing it disabled",
                            extra={"model_id": model_id, "base_url": self.base_url},
                        )
                        resolved = ResolvedPricing(
                            prompt=0.0,
                            completion=0.0,
                            context_length=None,
                            source="unresolved",
                        )
                        enabled = False
                    else:
                        enabled = True

                    # Prefer the source's own modality string (OpenRouter ships
                    # one, e.g. "text+image->text"); otherwise derive it from the
                    # captured input/output modalities in the same "in->out" shape
                    # rather than flattening vision models to "text->text".
                    modality = resolved.modality or (
                        f"{'+'.join(resolved.input_modalities)}"
                        f"->{'+'.join(resolved.output_modalities)}"
                    )

                    # A source can carry a price but no context (e.g. a litellm
                    # entry missing max_input_tokens); fall back to an id-based
                    # estimate so we never persist a zero-length window.
                    context_length = resolved.context_length or estimate_context_length(
                        model_id
                    )

                    spec_name = model_spec.get("name", model_name)
                    description = f"{spec_name}"
                    if owned_by != "unknown":
                        description += f" via {owned_by}"

                    models_list.append(
                        Model(
                            id=model_id,
                            name=spec_name,
                            created=created,
                            description=description,
                            context_length=context_length,
                            architecture=Architecture(
                                modality=modality,
                                input_modalities=resolved.input_modalities,
                                output_modalities=resolved.output_modalities,
                                tokenizer=resolved.tokenizer,
                                instruct_type=resolved.instruct_type,
                            ),
                            pricing=Pricing(
                                prompt=resolved.prompt,
                                completion=resolved.completion,
                                request=0.0,
                                image=0.0,
                                web_search=0.0,
                                internal_reasoning=0.0,
                                input_cache_read=resolved.input_cache_read,
                                input_cache_write=resolved.input_cache_write,
                            ),
                            sats_pricing=None,
                            per_request_limits=None,
                            top_provider=TopProvider(
                                context_length=context_length,
                                max_completion_tokens=(
                                    resolved.max_completion_tokens
                                    if resolved.max_completion_tokens is not None
                                    else context_length // 2
                                ),
                                is_moderated=bool(resolved.is_moderated),
                            ),
                            enabled=enabled,
                            upstream_provider_id=None,
                            canonical_slug=None,
                        )
                    )

                logger.info(
                    f"Fetched {len(models_list)} models from {self.upstream_name}",
                    extra={"model_count": len(models_list), "base_url": self.base_url},
                )
                return models_list

        except Exception as e:
            logger.error(
                f"Failed to fetch models from {self.upstream_name} API: {e}",
                extra={
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "base_url": self.base_url,
                },
            )
            return []
