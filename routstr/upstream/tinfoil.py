from __future__ import annotations

import math
from typing import TYPE_CHECKING

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse
from pydantic.v1 import BaseModel

from ..core.exceptions import UpstreamError
from ..core.logging import get_logger
from ..payment.models import Architecture, Model, Pricing, pricing_metadata
from .base import BaseUpstreamProvider
from .ehbp import (
    _ENCLAVE_URL_HEADER,
    _PROXY_ONLY_HEADERS,
    _RESPONSE_USAGE_HEADER,
    ConfidentialInferenceProfile,
    EHBPForwardingTarget,
)
from .pricing_resolver import FallbackPricingResolver, ResolvedPricing

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow

logger = get_logger(__name__)


class TinfoilModelPricing(BaseModel):
    inputTokenPricePer1M: float = 0.0
    outputTokenPricePer1M: float = 0.0
    requestPrice: float = 0.0


class TinfoilModel(BaseModel):
    id: str
    context_window: int = 0
    created: int = 0
    multimodal: bool = False
    reasoning: bool = False
    tool_calling: bool = False
    type: str = "chat"
    pricing: TinfoilModelPricing = TinfoilModelPricing()
    endpoints: list[str] = []


class TinfoilUpstreamProvider(BaseUpstreamProvider):
    """Direct upstream provider for the Tinfoil inference API.

    Tinfoil hosts open-source models inside attested secure enclaves and exposes
    an OpenAI-compatible API at ``https://inference.tinfoil.sh``. Request and
    response bodies are encrypted end-to-end with EHBP (HPKE), so Routstr acts
    as a blind relay: it forwards the opaque encrypted body, never sees
    plaintext, and bills from the ``X-Tinfoil-Usage-Metrics`` header that
    Tinfoil returns outside the encrypted body when
    ``X-Tinfoil-Request-Usage-Metrics: true`` is set.
    """

    provider_type = "tinfoil"
    default_base_url = "https://inference.tinfoil.sh"
    platform_url = "https://docs.tinfoil.sh"
    supports_ehbp = True
    confidential_inference_profile = ConfidentialInferenceProfile(
        usage_response_header=_RESPONSE_USAGE_HEADER,
        client_target_url_header=_ENCLAVE_URL_HEADER,
        allow_client_target_override=True,
        proxy_only_headers=_PROXY_ONLY_HEADERS,
    )

    def __init__(self, api_key: str, provider_fee: float = 1.0):
        super().__init__(
            base_url=self.default_base_url,
            api_key=api_key,
            provider_fee=provider_fee,
        )

    @classmethod
    def _build_from_row(
        cls, provider_row: "UpstreamProviderRow"
    ) -> "TinfoilUpstreamProvider":
        return cls(
            api_key=provider_row.api_key,
            provider_fee=provider_row.provider_fee,
        )

    @classmethod
    def get_provider_metadata(cls) -> dict[str, object]:
        return {
            "id": cls.provider_type,
            "name": "Tinfoil",
            "default_base_url": cls.default_base_url,
            "fixed_base_url": True,
            "platform_url": cls.platform_url,
            "can_create_account": False,
            "can_topup": False,
            "can_show_balance": False,
        }

    def transform_model_name(self, model_id: str) -> str:
        return model_id.removeprefix("tinfoil/")

    def get_confidential_inference_profile(self) -> ConfidentialInferenceProfile:
        return self.confidential_inference_profile

    async def forward_get_request(
        self,
        request: Request,
        path: str,
        headers: dict,
    ) -> Response | StreamingResponse:
        """Handle Tinfoil-specific GET endpoints.

        * ``/attestation`` (or ``/tee/attestation``): proxy to the Tinfoil ATC
          (attestation bundle proxy) at ``https://atc.tinfoil.sh/attestation``.
        * Other GETs: forward to the provider base URL
          (``https://inference.tinfoil.sh``). ``X-Tinfoil-Enclave-Url`` is an
          EHBP-only header used for encrypted POST requests and is not honored
          for unencrypted GET requests.
        """
        clean_path = path.removeprefix("tee/").rstrip("/")
        if clean_path == "attestation":
            return await self._proxy_attestation(headers)
        return await super().forward_get_request(request, path, headers)

    async def _proxy_attestation(self, headers: dict) -> Response:
        url = "https://atc.tinfoil.sh/attestation"
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(retries=1),
            timeout=30.0,
        ) as client:
            try:
                resp = await client.get(
                    url,
                    headers={
                        "Accept": headers.get("accept", "application/json"),
                    },
                )
                response_headers = dict(resp.headers)
                response_headers.pop("content-encoding", None)
                response_headers.pop("content-length", None)
                return Response(
                    content=resp.content,
                    status_code=resp.status_code,
                    headers=response_headers,
                )
            except Exception as exc:
                raise UpstreamError(
                    f"Error fetching Tinfoil attestation: {type(exc).__name__}",
                    status_code=502,
                ) from exc

    def get_ehbp_forwarding_target(
        self, path: str, model_obj: Model
    ) -> EHBPForwardingTarget:
        """Return the Tinfoil enclave target for EHBP requests.

        Requests usage metrics from the enclave so Routstr can bill exactly
        without decrypting the response body. The actual forwarding URL is
        overridden at dispatch time by ``X-Tinfoil-Enclave-Url`` when the SDK
        sends it (see ``routstr/upstream/ehbp.py``).
        """
        return EHBPForwardingTarget(
            url=f"{self.base_url.rstrip('/')}/{path.lstrip('/')}",
            headers={"X-Tinfoil-Request-Usage-Metrics": "true"},
            profile=self.confidential_inference_profile,
        )

    @staticmethod
    def _native_pricing(tf: TinfoilModel) -> tuple[float, float, float] | None:
        """Tinfoil's own per-1M rates, or ``None`` when it published no usable price.

        ``TinfoilModelPricing`` defaults every rate to ``0.0``, so a model whose
        catalog entry carries no pricing is indistinguishable from a free one:
        published as-is it would be served enabled and bill every request at
        nothing. Non-finite (``inf``/``NaN`` — ``json.loads`` accepts the bare
        literals and overflows ``1e999``) and negative rates are junk, not prices.
        Either way the caller falls through to the shared resolution chain rather
        than claiming ``native`` for a price Tinfoil did not give.
        """
        rates = (
            tf.pricing.inputTokenPricePer1M,
            tf.pricing.outputTokenPricePer1M,
            tf.pricing.requestPrice,
        )
        if any(not math.isfinite(rate) or rate < 0 for rate in rates):
            return None
        if not any(rate > 0 for rate in rates):
            return None
        return rates

    async def fetch_models(self) -> list[Model]:
        """Fetch models from the public Tinfoil models endpoint.

        ``GET /v1/models`` is unauthenticated and returns all available models
        with their pricing in USD per 1M tokens. A model Tinfoil did not price
        falls through to the shared litellm→OpenRouter chain and wears that
        source, or imports disabled as ``unresolved`` when nothing can price it.
        """
        url = f"{self.base_url}/v1/models"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url)
                response.raise_for_status()
                data = response.json()
                models_data = data.get("data", [])

                models: list[Model] = []
                resolver = FallbackPricingResolver()
                for model_data in models_data:
                    try:
                        tf = TinfoilModel.parse_obj(model_data)
                        native = self._native_pricing(tf)
                        enabled = True
                        if native is not None:
                            input_1m, output_1m, request_price = native
                            prompt_price = input_1m / 1_000_000
                            completion_price = output_1m / 1_000_000
                            cache_read = cache_write = 0.0
                            source = "native"
                        else:
                            resolved = await resolver.resolve(tf.id)
                            if resolved is None:
                                # Fail closed: never invent a price, and never
                                # publish Tinfoil's zero defaults as a real one.
                                logger.warning(
                                    f"No pricing source resolved for Tinfoil model "
                                    f"'{tf.id}'; importing it disabled",
                                    extra={
                                        "model_id": tf.id,
                                        "base_url": self.base_url,
                                    },
                                )
                                resolved = ResolvedPricing(
                                    prompt=0.0,
                                    completion=0.0,
                                    context_length=None,
                                    source="unresolved",
                                )
                                enabled = False
                            prompt_price = resolved.prompt
                            completion_price = resolved.completion
                            request_price = 0.0
                            cache_read = resolved.input_cache_read
                            cache_write = resolved.input_cache_write
                            source = resolved.source

                        modality = "text->text"
                        input_modalities = ["text"]
                        output_modalities = ["text"]
                        if tf.multimodal:
                            modality = "text->text+image"
                            input_modalities = ["text", "image"]

                        models.append(
                            Model(
                                id=tf.id,
                                name=tf.id,
                                created=tf.created,
                                description=f"Tinfoil {tf.type} model",
                                context_length=tf.context_window,
                                architecture=Architecture(
                                    modality=modality,
                                    input_modalities=input_modalities,
                                    output_modalities=output_modalities,
                                    tokenizer="Unknown",
                                    instruct_type=None,
                                ),
                                pricing=Pricing(
                                    prompt=prompt_price,
                                    completion=completion_price,
                                    request=request_price,
                                    image=0.0,
                                    web_search=0.0,
                                    internal_reasoning=0.0,
                                    input_cache_read=cache_read,
                                    input_cache_write=cache_write,
                                ),
                                enabled=enabled,
                                **pricing_metadata(source),
                            )
                        )
                    except Exception as e:
                        logger.warning(
                            "Failed to parse Tinfoil model",
                            extra={
                                "model_id": model_data.get("id", "unknown"),
                                "error": str(e),
                                "error_type": type(e).__name__,
                            },
                        )

                return models
        except Exception as e:
            logger.error(
                "Error fetching models from Tinfoil",
                extra={"error": str(e), "error_type": type(e).__name__},
            )
            return []
