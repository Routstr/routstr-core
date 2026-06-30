from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse
from pydantic.v1 import BaseModel

from ..core.exceptions import UpstreamError
from ..core.logging import get_logger
from ..payment.models import Architecture, Model, Pricing
from .base import BaseUpstreamProvider
from .ehbp import (
    _ENCLAVE_URL_HEADER,
    _PROXY_ONLY_HEADERS,
    _RESPONSE_USAGE_HEADER,
    ConfidentialInferenceProfile,
    EHBPForwardingTarget,
)

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
        protocol="EHBP",
        usage_response_header=_RESPONSE_USAGE_HEADER,
        client_target_url_header=_ENCLAVE_URL_HEADER,
        allow_client_target_override=True,
        trusted_model_binding_header=None,
        missing_usage_billing_policy="max_cost",
        proxy_only_headers=_PROXY_ONLY_HEADERS,
    )

    def __init__(self, api_key: str, provider_fee: float = 1.0):
        super().__init__(
            base_url=self.default_base_url,
            api_key=api_key,
            provider_fee=provider_fee,
        )

    @classmethod
    def from_db_row(
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
        clean_path = path.removeprefix("tee/")
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

    async def fetch_models(self) -> list[Model]:
        """Fetch models from the public Tinfoil models endpoint.

        ``GET /v1/models`` is unauthenticated and returns all available models
        with their pricing in USD per 1M tokens.
        """
        url = f"{self.base_url}/v1/models"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url)
                response.raise_for_status()
                data = response.json()
                models_data = data.get("data", [])

                models: list[Model] = []
                for model_data in models_data:
                    try:
                        tf = TinfoilModel.parse_obj(model_data)
                        input_price = tf.pricing.inputTokenPricePer1M
                        output_price = tf.pricing.outputTokenPricePer1M
                        request_price = tf.pricing.requestPrice

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
                                    prompt=input_price / 1_000_000,
                                    completion=output_price / 1_000_000,
                                    request=request_price,
                                    image=0.0,
                                    web_search=0.0,
                                    internal_reasoning=0.0,
                                ),
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
