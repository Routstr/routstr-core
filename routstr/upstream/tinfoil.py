from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Optional

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse
from pydantic.v1 import BaseModel, validator

from ..core.error_scope import (
    ERROR_SCOPE_HEADER,
    ERROR_SCOPE_UPSTREAM,
    UPSTREAM_ERROR_STATUS,
    UPSTREAM_UNAVAILABLE,
)
from ..core.exceptions import UpstreamError
from ..core.logging import get_logger
from ..payment.models import Architecture, Model, Pricing, Reasoning
from .base import BaseUpstreamProvider
from .ehbp import (
    _ENCLAVE_URL_HEADER,
    _PROXY_ONLY_HEADERS,
    _RESPONSE_USAGE_HEADER,
    ConfidentialInferenceProfile,
    EHBPForwardingTarget,
)
from .reasoning_effort import EFFORT_RANK

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow

logger = get_logger(__name__)


class TinfoilModelPricing(BaseModel):
    inputTokenPricePer1M: float = 0.0
    outputTokenPricePer1M: float = 0.0
    cachedInputTokenPricePer1M: Optional[float] = None
    requestPrice: float = 0.0


class TinfoilReasoningParams(BaseModel):
    """Reasoning metadata Tinfoil publishes for a model.

    ``effort_map`` translates a caller-facing level (``low``/``medium``/``high``)
    into the value the model's chat template accepts — for example DeepSeek V4.1
    maps ``medium`` to ``high`` and rejects a literal ``medium``. ``params``
    documents the per-route enable/disable body shapes, with ``$EFFORT`` standing
    in for the mapped value.

    Both fields are typed permissively on purpose: this is upstream metadata, and
    a shape change here must not fail ``TinfoilModel`` parsing and drop the model
    from the catalog. Only well-formed pairs survive into ``Reasoning``.
    """

    effort_map: dict[str, Any] | None = None
    params: dict[str, Any] | None = None

    @validator("effort_map", "params", pre=True)
    def _object_or_none(cls, value: object) -> object:  # noqa: N805
        return value if isinstance(value, dict) else None


class TinfoilModel(BaseModel):
    id: str
    context_window: int = 0
    created: int = 0
    multimodal: bool = False
    reasoning: bool = False
    # Declared for completeness; the quality scores have no ``Reasoning`` field.
    intelligence: dict[str, Any] | None = None
    reasoning_params: TinfoilReasoningParams | None = None
    tool_calling: bool = False
    type: str = "chat"
    pricing: TinfoilModelPricing = TinfoilModelPricing()
    endpoints: list[str] = []

    @validator("intelligence", "reasoning_params", pre=True)
    def _metadata_or_none(cls, value: object) -> object:  # noqa: N805
        """Tolerate a metadata shape change instead of dropping the model."""
        return value if isinstance(value, (dict, TinfoilReasoningParams)) else None


def _clean_effort_map(
    reasoning_params: TinfoilReasoningParams | None,
) -> dict[str, str]:
    """Return only caller-level -> native-effort pairs that are usable."""
    if reasoning_params is None or not reasoning_params.effort_map:
        return {}
    return {
        level: effort
        for level, effort in reasoning_params.effort_map.items()
        if isinstance(level, str)
        and level.strip()
        and isinstance(effort, str)
        and effort.strip()
    }


def _ordered_efforts(effort_map: dict[str, str]) -> list[str]:
    """Native effort values, deduplicated and highest-first."""
    values = list(dict.fromkeys(effort_map.values()))
    ranked = [effort for effort in EFFORT_RANK if effort in values]
    ranked.extend(effort for effort in values if effort not in EFFORT_RANK)
    return ranked


def _reasoning_disableable(
    reasoning_params: TinfoilReasoningParams | None,
) -> bool:
    """True when Tinfoil publishes a disable shape for any route."""
    params = reasoning_params.params if reasoning_params else None
    if not isinstance(params, dict):
        return False
    return any(
        isinstance(route, dict) and "disable" in route for route in params.values()
    )


def _tinfoil_reasoning(tf: TinfoilModel) -> Reasoning | None:
    """Translate a Tinfoil model's reasoning metadata into ``Reasoning``.

    Tinfoil publishes an effort *map* rather than a flat allowlist, and no
    default effort, so ``default_effort``/``default_enabled`` stay unset instead
    of inventing values. ``supported_efforts`` lists the native values a caller
    can send as-is (highest first); clients that offer the coarser
    ``low``/``medium``/``high`` levels use ``effort_map`` for the exact spelling.

    Models with no effort map (reasoning is a plain on/off toggle there) publish
    no ``reasoning`` object, matching how Routstr already treats models without
    effort metadata.
    """
    if not tf.reasoning:
        return None
    effort_map = _clean_effort_map(tf.reasoning_params)
    if not effort_map:
        return None
    return Reasoning(
        supported_efforts=_ordered_efforts(effort_map),
        effort_map=effort_map,
        # A published disable shape proves reasoning is optional; without one the
        # answer is unknown rather than assumed mandatory.
        mandatory=False if _reasoning_disableable(tf.reasoning_params) else None,
    )


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
    # Tinfoil's router serves only its versioned API surface (``/v1/...``), and
    # unlike most providers its base URL does not carry the prefix —
    # ``fetch_models`` adds ``/v1`` explicitly for ``GET /v1/models``. The EHBP
    # target must re-add it: a client that posts to ``/chat/completions``
    # otherwise reaches ``https://inference.tinfoil.sh/chat/completions`` and
    # gets a 404 ("Not found.") from the router.
    ehbp_path_prefix = "v1"
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
                if resp.status_code >= 500:
                    logger.warning(
                        "Tinfoil attestation upstream returned %s",
                        resp.status_code,
                        extra={"status_code": resp.status_code},
                    )
                    return Response(
                        content=json.dumps(
                            {
                                "error": {
                                    "type": "upstream_error",
                                    "code": UPSTREAM_UNAVAILABLE,
                                    "message": (
                                        "Attestation upstream returned "
                                        f"{resp.status_code}"
                                    ),
                                    "upstream_status": resp.status_code,
                                }
                            }
                        ),
                        status_code=UPSTREAM_ERROR_STATUS,
                        media_type="application/json",
                        headers={ERROR_SCOPE_HEADER: ERROR_SCOPE_UPSTREAM},
                    )
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
            url=(
                f"{self.base_url.rstrip('/')}/"
                f"{self.build_ehbp_request_path(path, model_obj)}"
            ),
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

                        # Tinfoil bills cache reads at the cached rate when the
                        # model exposes one, otherwise at the full input rate.
                        # Cache writes are never priced separately — a miss is
                        # just regular input prefill.
                        cached_price = tf.pricing.cachedInputTokenPricePer1M
                        if cached_price is None or cached_price <= 0.0:
                            cached_price = input_price

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
                                    input_cache_read=cached_price / 1_000_000,
                                    input_cache_write=input_price / 1_000_000,
                                ),
                                reasoning=_tinfoil_reasoning(tf),
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
