from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, Optional, Sequence

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
from .tinfoil_trailer import _strip_hop_by_hop_headers

if TYPE_CHECKING:
    from ..core.db import UpstreamProviderRow

logger = get_logger(__name__)

# A visible request header the enclave uses to override the output cap of the
# sealed (EHBP) body: tinfoilsh/confidential-model-router#518 maps it onto
# ``max_completion_tokens`` on ``/v1/chat/completions`` (dropping ``max_tokens``)
# and onto ``max_output_tokens`` on ``/v1/responses``. It is the only completion
# ceiling the node can read on a sealed request, which is what makes a smaller
# reservation than the model's whole context possible.
_TINFOIL_MAX_OUTPUT_TOKENS_HEADER = "X-Tinfoil-Max-Output-Tokens"

# Whether a sealed request may be quoted from the enforced header at all.
#
# The header is not a *request* bound, so this is off. The engine applies the cap
# per choice and the router forwards ``n``/``best_of`` untouched, so a sealed
# body can hide ``n`` and bill ``n * cap``. Measured on router-0.tinfoil.sh
# (deepseek-v4-1-flash, header = 8): n=1 -> 8 completion tokens, n=2 -> 16,
# n=3 -> 24, header=200/n=2 -> 400; prompt tokens counted once. The node cannot
# see ``n`` inside a sealed body, so nothing server-side bounds that fan-out and
# any capped hold would be a fiction.
#
# Every piece below is kept, tested scaffolding: the parser, the route gate, the
# capped worst-case math and the /v1/info advertisement are exercised by
# ``tests/unit/test_tinfoil_max_output_tokens_reservation.py`` with this constant
# patched on. Activation is a code change gated on the *upstream* guarantee —
# confidential-model-router rejecting ``n``/``best_of`` > 1 when the header is
# supplied, or dividing the budget across choices — never an operator toggle and
# never a client-declared promise: a seal hides the body from the node, so trust
# has to come from the enclave enforcing the total.
EHBP_OUTPUT_TOKEN_RESERVATION_ENABLED = False

# Candidate routes for a future activation — NOT a claim that any is proven
# sound today. The cap is **per choice**, not per request:
#
#   probe (router-0.tinfoil.sh, deepseek-v4-1-flash, header=8)
#     n=1 -> completion_tokens=8;  n=2 -> 16;  n=3 -> 24;  header=200/n=2 -> 400
#   while ``best_of=3`` with header=8 -> completion_tokens=8
#
# so a sealed chat body hiding ``n`` can bill an unbounded multiple of the quoted
# cap. ``n``/``best_of`` are invisible on a sealed body, so no server-side check
# can bound that fan-out; chat completions is therefore excluded.
#
# ``/v1/responses`` is the only plausible candidate: it has neither ``n`` nor
# ``best_of``, and one probe showed an injected ``n: 3`` ignored there
# (output_tokens=8, one output) while the header still overrides
# ``max_output_tokens`` in both directions (body=100/header=8 -> 8;
# body=8/header=100 -> 100). That observation is **one request shape, not a
# proof**: other budget paths — tool/server-side loops, multi-turn agentic runs,
# provider-side retries, knobs not exercised by the probe — could still multiply
# the billed output past the cap. So even Responses stays UNVERIFIED and nothing
# is quoted while ``EHBP_OUTPUT_TOKEN_RESERVATION_ENABLED`` is False. Widening
# this set requires a measured, request-total guarantee from the upstream.
#
# ``/v1/completions`` is excluded for a second reason: deployed enclaves answer it
# with 404, so its mapping has never been observed live.
_TINFOIL_MAX_OUTPUT_TOKENS_ROUTES = frozenset({"responses"})

# The enclave parses the value with a signed 64-bit base-10 ParseInt. Bounding
# it here keeps a caller-supplied digit string from reaching ``int()`` at all,
# and anything above a model's context window yields no discount anyway.
_TINFOIL_MAX_OUTPUT_TOKENS_LIMIT = 2**31 - 1

_CANONICAL_POSITIVE_INT = re.compile(r"[1-9][0-9]*\Z")

# Public, stable contract: the canonical routes this provider quotes an
# enforced output-token cap on. ``/v1/info`` advertises exactly this list so a
# client funds a capped hold only where the node can prove the ceiling bounds
# the whole request (see ``_TINFOIL_MAX_OUTPUT_TOKENS_ROUTES``).
EHBP_OUTPUT_TOKEN_RESERVATION_ROUTES = _TINFOIL_MAX_OUTPUT_TOKENS_ROUTES


def _canonical_route(path: str) -> str:
    """Collapse an API path onto the key the header's route table uses.

    The proxy screens the path before routing, so this only has to absorb the
    spellings an OpenAI-style client may use: a leading slash, a ``v1/``
    prefix, or a trailing slash.
    """
    return path.split("?", 1)[0].strip("/").removeprefix("v1/")


def _header_field_values(headers: object, name: str) -> list[str]:
    """Every field value for ``name``, in wire order and case-insensitively.

    Starlette's ``Headers`` keeps repeated field lines reachable through
    ``getlist``, which is what lets a repeated field be detected at all. A plain
    mapping (how upstream code hands headers around) has already collapsed its
    duplicates, so it is scanned case-insensitively instead of by lookup — HTTP
    field names are not case-sensitive, and a ``dict`` would otherwise miss
    ``x-tinfoil-max-output-tokens`` spelled any other way.
    """
    getlist = getattr(headers, "getlist", None)
    if callable(getlist):
        return [str(value) for value in getlist(name)]
    items = getattr(headers, "items", None)
    if not callable(items):
        return []
    wanted = name.lower()
    return [
        str(value)
        for key, value in items()
        if str(key).lower() == wanted and value is not None
    ]


def _cap_header_would_be_dropped(headers: object) -> bool:
    """True when the forwarding sanitizer would strip the cap header.

    The EHBP request is re-framed onto a new upstream connection, so
    ``forward_with_trailer`` removes hop-by-hop headers — including every header
    a ``Connection`` field nominates. If that nomination names the cap header,
    the enclave never receives it while the node quoted against it, leaving the
    output unbounded against a smaller hold. Quote nothing in that case.

    The check runs the *real* sanitizer over the request headers rather than
    reimplementing its rules, so a future change to the forwarding path cannot
    silently invalidate the quote. Provider-added target headers are merged
    after this stripping step, so a provider must never re-add this header from
    its own target set (Tinfoil's adds only usage metrics); if one ever does,
    this hook has to take the merged headers instead.
    """
    cap_name = _TINFOIL_MAX_OUTPUT_TOKENS_HEADER.lower()
    for value in _header_field_values(headers, "Connection"):
        for token in value.split(","):
            if token.strip().lower() == cap_name:
                return True

    items = getattr(headers, "items", None)
    if not callable(items):
        return False
    outgoing: dict[str, str] = {}
    for key, value in items():
        if value is not None:
            outgoing[str(key)] = str(value)
    sanitized = _strip_hop_by_hop_headers(outgoing)
    return not any(str(key).lower() == cap_name for key in sanitized)


def parse_max_output_tokens_header(values: Sequence[str]) -> int | None:
    """Return the output ceiling a visible Tinfoil header declares, if any.

    Deliberately strict, because the value becomes a *quote* the reservation is
    sized against: exactly one field value, ASCII-whitespace-stripped, matching
    ``[1-9][0-9]*`` and within ``2**31 - 1``. Anything else — repeated values, a
    comma-joined list, zero, a sign, a fraction, an empty field — yields ``None``
    and the request keeps the full reserve.

    The header itself is still forwarded verbatim, so the enclave remains the
    authority that rejects a malformed value with its own 400. Being stricter
    than the enclave here can only over-reserve, never under-cover.
    """
    if len(values) != 1:
        # No field line, or several (repeated header / comma-joined list). The
        # enclave rejects repeats rather than picking one, so no cap is quoted.
        return None
    raw = values[0].strip(" \t")
    if len(raw) > len(str(_TINFOIL_MAX_OUTPUT_TOKENS_LIMIT)):
        return None
    if not _CANONICAL_POSITIVE_INT.match(raw):
        return None
    cap = int(raw)
    return cap if 1 <= cap <= _TINFOIL_MAX_OUTPUT_TOKENS_LIMIT else None


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


def _reasoning_mandatory(tf: TinfoilModel) -> bool | None:
    """Whether the model reasons without being asked to.

    ``intelligence`` is keyed by the levels the model supports, including the
    special level ``off`` when reasoning can be turned off at all. Levels
    without ``off`` mean the model always reasons: GLM 5.3, Kimi K3 and
    gpt-oss-120b publish no ``off`` and no disable shape, so a caller cannot
    ask them for a non-reasoning answer.

    Without either signal the answer is unknown rather than assumed, so a
    shape change upstream cannot silently flip the flag.
    """
    intelligence = tf.intelligence if isinstance(tf.intelligence, dict) else {}
    if intelligence and "off" not in intelligence:
        return True
    if _reasoning_disableable(tf.reasoning_params):
        return False
    return None


def _tinfoil_reasoning(tf: TinfoilModel) -> Reasoning | None:
    """Translate a Tinfoil model's reasoning metadata into ``Reasoning``.

    Tinfoil publishes an effort *map*, not an allowlist: the keys are the
    caller-facing levels a UI offers (``low``/``medium``/``high``), the values
    the exact spelling the model's chat template accepts. It is therefore
    published as ``effort_map`` only, never as ``supported_efforts``.

    That distinction matters downstream: ``supported_efforts`` is not merely
    advertised, it is applied - ``routstr.upstream.reasoning_effort`` snaps any
    requested level onto the nearest member, which is what kept turning a
    deliberate ``reasoning_effort: "none"`` into ``"low"`` (reasoning on) and
    downgrading ``"max"``. Tinfoil accepts more than the map's values anyway
    (``none``, ``max``, and integers inside ``chat_template_kwargs``), so no
    truthful allowlist exists to publish.

    Defaults stay unset because the catalog publishes none, and a model whose
    reasoning is a plain toggle publishes no effort map - only ``mandatory``,
    when it can be determined.
    """
    if not tf.reasoning:
        return None
    effort_map = _clean_effort_map(tf.reasoning_params)
    mandatory = _reasoning_mandatory(tf)
    if not effort_map and mandatory is None:
        return None
    return Reasoning(effort_map=effort_map or None, mandatory=mandatory)


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

    def _apply_reasoning_effort(self, data: dict, model_obj: Model) -> bool:
        """Rescue effort levels the gateway accepts but the model rejects.

        Tinfoil's gateway advertises the OpenAI literals
        (``none|minimal|low|medium|high|xhigh|max``), while a model's chat
        template can be narrower: DeepSeek V4.1 and GLM 5.3 reject a literal
        ``medium``. Translate only those levels, using the model's published
        ``effort_map``; every other value is already native and is left exactly
        as sent.

        Deliberately narrow: ``none`` is a valid, meaningful input (it disables
        reasoning) and ``max`` is a stronger level than ``xhigh``, so a rewrite
        that reshaped either - which snapping onto an allowlist did - silently
        changed what the caller asked for.

        Only readable bodies reach here. EHBP requests are sealed ciphertext, so
        the routing client applies the same map before encrypting.
        """
        reasoning = getattr(model_obj, "reasoning", None)
        effort_map = (reasoning.effort_map if reasoning else None) or {}
        if not effort_map:
            return False
        native = set(effort_map.values())
        changed = False
        for holder, key in (
            (data, "reasoning_effort"),
            (data.get("reasoning"), "effort"),
            (data.get("chat_template_kwargs"), "reasoning_effort"),
        ):
            if not isinstance(holder, dict):
                continue
            value = holder.get(key)
            if not isinstance(value, str):
                continue
            effort = value.strip().lower()
            if effort in native:
                continue
            mapped = effort_map.get(effort)
            if mapped is None or mapped == value:
                continue
            holder[key] = mapped
            changed = True
        return changed

    def get_confidential_inference_profile(self) -> ConfidentialInferenceProfile:
        return self.confidential_inference_profile

    def enforced_completion_cap(self, headers: object, path: str) -> int | None:
        """Return the output ceiling this enclave enforces for a sealed request.

        ``X-Tinfoil-Max-Output-Tokens`` is visible to the enclave while the body
        stays sealed, and the enclave overrides the body's caps with it in both
        directions, so the value is a per-choice output ceiling Routstr can read
        without decrypting anything. Whether that ceiling also bounds the *whole*
        request is UNVERIFIED (one probe suggests Responses ignores an injected
        ``n``, but other budget paths may still multiply output), so nothing is
        quoted while ``EHBP_OUTPUT_TOKEN_RESERVATION_ENABLED`` is False. See
        ``_TINFOIL_MAX_OUTPUT_TOKENS_ROUTES`` for why chat completions is not a
        candidate. The header is forwarded regardless (it is not a proxy-only
        header): the enclave, not this method, decides whether a value is
        acceptable.
        """
        if not EHBP_OUTPUT_TOKEN_RESERVATION_ENABLED:
            return None
        if _canonical_route(path) not in _TINFOIL_MAX_OUTPUT_TOKENS_ROUTES:
            return None
        if _cap_header_would_be_dropped(headers):
            # The quote would describe a header the enclave never sees.
            return None
        return parse_max_output_tokens_header(
            _header_field_values(headers, _TINFOIL_MAX_OUTPUT_TOKENS_HEADER)
        )

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
