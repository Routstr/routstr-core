import asyncio
import json
import random

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel as V2BaseModel
from pydantic.v1 import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession

from ..core.db import ModelRow, UpstreamProviderRow, get_session
from ..core.logging import get_logger
from ..core.settings import settings
from .price import sats_usd_price

logger = get_logger(__name__)

models_router = APIRouter()

_MODEL_TEST_ENDPOINT_PATHS = {
    "chat-completions": "chat/completions",
    "completions": "completions",
    "embeddings": "embeddings",
    "responses": "responses",
}

# Cap the caller-supplied test payload to avoid forwarding oversized bodies
# upstream on the operator's credentials.
_MODEL_TEST_MAX_REQUEST_BYTES = 64 * 1024


async def _require_admin_api(request: Request) -> None:
    """Require admin auth without creating an import-time cycle with core.admin."""
    from ..core.admin import require_admin_api

    await require_admin_api(request)


class Architecture(BaseModel):
    modality: str
    input_modalities: list[str]
    output_modalities: list[str]
    tokenizer: str
    instruct_type: str | None


class Pricing(BaseModel):
    prompt: float
    completion: float
    request: float = 0.0
    image: float = 0.0
    web_search: float = 0.0
    internal_reasoning: float = 0.0
    input_cache_read: float = 0.0
    input_cache_write: float = 0.0
    max_prompt_cost: float = 0.0  # in sats not msats
    max_completion_cost: float = 0.0  # in sats not msats
    max_cost: float = 0.0  # in sats not msats


class TopProvider(BaseModel):
    context_length: int | None = None
    max_completion_tokens: int | None = None
    is_moderated: bool | None = None


class Model(BaseModel):
    id: str
    name: str
    created: int
    description: str
    context_length: int
    architecture: Architecture
    pricing: Pricing
    sats_pricing: Pricing | None = None
    per_request_limits: dict | None = None
    top_provider: TopProvider | None = None
    enabled: bool = True
    upstream_provider_id: int | str | None = None
    canonical_slug: str | None = None
    alias_ids: list[str] | None = None
    forwarded_model_id: str | None = None

    def __hash__(self) -> int:
        return hash(self.id)


def _litellm_entry(*keys: str) -> dict | None:
    """First litellm cost-map entry matching any of the given exact keys."""
    import litellm

    for key in keys:
        candidate = litellm.model_cost.get(key)
        if isinstance(candidate, dict):
            return candidate
    return None


def _family_tokens(lowered_id: str) -> list[str]:
    """Candidate family tokens derived from an id, most-specific first.

    The provider prefix (``deepseek`` in ``deepseek/x``) and the leading segment
    of the model name (``deepseek`` in ``deepseek-v4-flash``). Tokens shorter
    than 4 chars are dropped so short noise like ``gpt`` cannot match unrelated
    litellm keys.
    """
    provider, sep, rest = lowered_id.partition("/")
    name = rest if sep else provider
    tokens: list[str] = []
    if sep and len(provider) >= 4:
        tokens.append(provider)
    leading = name.split("-", 1)[0]
    if len(leading) >= 4 and leading not in tokens:
        tokens.append(leading)
    return tokens


def _family_reference(lowered_id: str) -> dict | None:
    """First litellm entry sharing a family token AND carrying a cache-read rate.

    Generic, data-driven fallback for vanity ids that proxies invent
    (``deepseek-v4-flash``) which match no litellm key directly. Any provider
    whose litellm snapshots price cache reads (deepseek, anthropic, ...) is
    matched without a hand-maintained family list. The family token must be the
    key's root segment (``deepseek/...`` / ``deepseek-...``), not appear anywhere
    in it — that excludes reseller-prefixed snapshots (``deepinfra/deepseek/...``,
    ``novita/deepseek/...``) whose cache markup differs from the native provider.
    Keys are scanned in sorted order so the chosen reference is deterministic.
    """
    import litellm

    for token in _family_tokens(lowered_id):
        for key in sorted(litellm.model_cost):
            lowered_key = key.lower()
            if not (
                lowered_key == token
                or lowered_key.startswith(token + "/")
                or lowered_key.startswith(token + "-")
            ):
                continue
            entry = litellm.model_cost.get(key)
            if not isinstance(entry, dict):
                continue
            ref_input = entry.get("input_cost_per_token")
            ref_read = entry.get("cache_read_input_token_cost")
            if (
                isinstance(ref_input, (int, float))
                and ref_input > 0
                and isinstance(ref_read, (int, float))
                and ref_read > 0
            ):
                return entry
    return None


def backfill_cache_pricing(model_id: str, pricing: Pricing) -> Pricing:
    """Fill missing cache rates from litellm's bundled cost map.

    The OpenRouter model feed omits ``input_cache_read`` / ``input_cache_write``
    for many models. Without a cache rate, billing falls back to the full input
    rate, overcharging cache reads (DeepSeek hits are ~10x cheaper). Two
    strategies are tried in order:

    1. **Exact match** — litellm ships absolute per-token USD rates keyed by the
       upstream id (``deepseek/deepseek-chat``) or its bare model name
       (``gpt-4o``); both spellings are tried and copied directly.
    2. **Family ratio** — vanity ids proxies invent (``deepseek-v4-flash``) match
       no litellm key. A reference entry for the same provider/family is found by
       generic scan (no hand-maintained list) and its ``cache_read/input``
       *ratio* is scaled by THIS model's own input price — correct even when the
       vanity model is priced differently from the reference.

    Rates already present (e.g. provided by OpenRouter) are authoritative and
    never overwritten. Models matching no family are returned unchanged.
    """
    needs_read = (pricing.input_cache_read or 0.0) <= 0.0
    needs_write = (pricing.input_cache_write or 0.0) <= 0.0
    if not (needs_read or needs_write):
        return pricing

    updated = Pricing.parse_obj(pricing.dict())

    # 1. Exact match: absolute per-token USD rates apply directly.
    info = _litellm_entry(model_id, model_id.split("/", 1)[-1])
    if info is not None:
        if needs_read:
            read_rate = info.get("cache_read_input_token_cost")
            if isinstance(read_rate, (int, float)) and read_rate > 0:
                updated.input_cache_read = float(read_rate)
                needs_read = False
        if needs_write:
            write_rate = info.get("cache_creation_input_token_cost")
            if isinstance(write_rate, (int, float)) and write_rate > 0:
                updated.input_cache_write = float(write_rate)
                needs_write = False

    if not (needs_read or needs_write):
        return updated

    # 2. Family ratio for vanity ids (or fields the exact entry left unpriced).
    if pricing.prompt <= 0.0:
        return updated
    reference = _family_reference(model_id.lower())
    if reference is None:
        logger.debug(
            "No litellm cache-rate reference for model family",
            extra={"model_id": model_id},
        )
        return updated
    ref_input = reference.get("input_cost_per_token")
    if not isinstance(ref_input, (int, float)) or ref_input <= 0:
        return updated
    if needs_read:
        ref_read = reference.get("cache_read_input_token_cost")
        if isinstance(ref_read, (int, float)) and ref_read > 0:
            updated.input_cache_read = pricing.prompt * (ref_read / ref_input)
    if needs_write:
        ref_write = reference.get("cache_creation_input_token_cost")
        if isinstance(ref_write, (int, float)) and ref_write > 0:
            updated.input_cache_write = pricing.prompt * (ref_write / ref_input)
    return updated


def _has_valid_pricing(model: dict) -> bool:
    """Check if model has valid pricing (not free, no negative values)."""
    pricing = model.get("pricing", {})
    if not pricing:
        return False

    try:
        prompt = float(pricing.get("prompt", 0))
        completion = float(pricing.get("completion", 0))
    except (ValueError, TypeError):
        return False

    if prompt < 0 or completion < 0:
        return False

    if prompt == 0 and completion == 0:
        return False

    return True


async def async_fetch_openrouter_models(source_filter: str | None = None) -> list[dict]:
    """Asynchronously fetch model information from OpenRouter API."""
    base_url = "https://openrouter.ai/api/v1"

    try:
        async with httpx.AsyncClient() as client:
            models_response, embeddings_response = await asyncio.gather(
                client.get(f"{base_url}/models", timeout=30),
                client.get(f"{base_url}/embeddings/models", timeout=30),
                return_exceptions=True,
            )

            def process_models_response(
                response: httpx.Response | BaseException,
            ) -> list[dict]:
                if not isinstance(response, BaseException):
                    response.raise_for_status()
                    data = response.json()
                    return [
                        model
                        for model in data.get("data", [])
                        if ":free" not in model.get("id", "").lower()
                    ]
                return []

            models_data: list[dict] = []
            models_data.extend(process_models_response(models_response))
            models_data.extend(process_models_response(embeddings_response))

            # Apply source filter and exclusions
            filtered_models = []
            for model in models_data:
                model_id = model.get("id", "")

                if source_filter:
                    source_prefix = f"{source_filter}/"
                    if not model_id.startswith(source_prefix):
                        continue

                    model = dict(model)
                    model["id"] = model_id[len(source_prefix) :]
                    model_id = model["id"]

                if "(free)" in model.get("name", ""):
                    continue

                if not _has_valid_pricing(model):
                    continue

                filtered_models.append(model)

            return filtered_models
    except Exception as e:
        logger.error(f"Error (async) fetching models from OpenRouter API: {e}")
        return []


def _row_to_model(
    row: ModelRow, apply_provider_fee: bool = False, provider_fee: float = 1.01
) -> Model:
    architecture = json.loads(row.architecture)
    pricing = json.loads(row.pricing)
    per_request_limits = (
        json.loads(row.per_request_limits) if row.per_request_limits else None
    )
    top_provider_dict = json.loads(row.top_provider) if row.top_provider else None

    if apply_provider_fee and isinstance(pricing, dict):
        pricing = {k: float(v) * provider_fee for k, v in pricing.items()}

    if isinstance(pricing, dict) and float(pricing.get("request", 0.0)) <= 0.0:
        pricing["request"] = max(pricing.get("request", 0.0), 0.0)

    parsed_pricing = Pricing.parse_obj(pricing)
    model = Model(
        id=row.id,
        name=row.name,
        created=row.created,
        description=row.description,
        context_length=row.context_length,
        architecture=Architecture.parse_obj(architecture),
        pricing=parsed_pricing,
        sats_pricing=None,
        per_request_limits=per_request_limits,
        top_provider=TopProvider.parse_obj(top_provider_dict)
        if top_provider_dict
        else None,
        enabled=row.enabled,
        upstream_provider_id=row.upstream_provider_id,
        canonical_slug=getattr(row, "canonical_slug", None),
        alias_ids=json.loads(row.alias_ids) if row.alias_ids else None,
        forwarded_model_id=getattr(row, "forwarded_model_id", None) or row.id,
    )

    if apply_provider_fee:
        (
            parsed_pricing.max_prompt_cost,
            parsed_pricing.max_completion_cost,
            parsed_pricing.max_cost,
        ) = _calculate_usd_max_costs(model)

    try:
        sats_to_usd = sats_usd_price()
        model = _update_model_sats_pricing(model, sats_to_usd)
    except Exception as e:
        logger.warning(f"Could not calculate sats pricing: {e}")

    return model


async def list_models(
    session: AsyncSession,
    upstream_id: int,
    include_disabled: bool = False,
    apply_fees: bool = True,
) -> list[Model]:
    from sqlmodel import select

    from ..core.db import UpstreamProviderRow

    query = select(ModelRow)
    if upstream_id is not None:
        query = query.where(ModelRow.upstream_provider_id == upstream_id)
    if not include_disabled:
        query = query.where(ModelRow.enabled)

    rows = (await session.exec(query)).all()  # type: ignore
    provider_result = await session.exec(select(UpstreamProviderRow))
    providers_by_id = {p.id: p for p in provider_result.all()}
    return [
        _row_to_model(
            r,
            apply_provider_fee=apply_fees,
            provider_fee=providers_by_id[r.upstream_provider_id].provider_fee
            if r.upstream_provider_id in providers_by_id
            else 1.01,
        )
        for r in rows
        if include_disabled
        or (
            r.upstream_provider_id in providers_by_id
            and providers_by_id[r.upstream_provider_id].enabled
        )
    ]


def _calculate_usd_max_costs(model: Model) -> tuple[float, float, float]:
    """Calculate max costs in USD based on model context/token limits.

    Args:
        model: Model object

    Returns:
        Tuple of (max_prompt_cost, max_completion_cost, max_cost) in USD
    """
    min_req_msat = max(1, int(getattr(settings, "min_request_msat", 1)))
    min_req_usd = float(min_req_msat) / 1_000_000.0

    prompt_price = model.pricing.prompt
    completion_price = model.pricing.completion

    if model.top_provider and (
        model.top_provider.context_length or model.top_provider.max_completion_tokens
    ):
        if (cl := model.top_provider.context_length) and (
            mct := model.top_provider.max_completion_tokens
        ):
            if cl <= mct:
                return (
                    cl * prompt_price,
                    cl * completion_price,
                    cl * max(completion_price, prompt_price),
                )
            return (
                cl * prompt_price,
                mct * completion_price,
                (cl - mct) * prompt_price + mct * completion_price,
            )
        elif cl := model.top_provider.context_length:
            return (
                cl * prompt_price,
                cl * completion_price,
                cl * max(completion_price, prompt_price),
            )
        elif mct := model.top_provider.max_completion_tokens:
            return (
                mct * prompt_price,
                mct * completion_price,
                mct * completion_price,
            )
    elif model.context_length:
        return (
            model.context_length * prompt_price,
            model.context_length * completion_price,
            model.context_length * max(completion_price, prompt_price),
        )

    p = prompt_price * 1_000_000
    c = completion_price * 32_000
    r = model.pricing.request * 100_000
    i = model.pricing.image * 100
    w = model.pricing.web_search * 1000
    ir = model.pricing.internal_reasoning * 100
    return (p, c, max(p + c + r + i + w + ir, min_req_usd))


def _update_model_sats_pricing(model: Model, sats_to_usd: float) -> Model:
    """Update a model's sats_pricing based on USD pricing and exchange rate.

    Args:
        model: Model object to update
        sats_to_usd: Current sats to USD exchange rate

    Returns:
        Updated Model object with new sats_pricing
    """
    try:
        min_req_msat = max(1, int(getattr(settings, "min_request_msat", 1)))
        min_req_sats = float(min_req_msat) / 1000.0

        sats = Pricing.parse_obj(
            {k: v / sats_to_usd for k, v in model.pricing.dict().items()}
        )

        if sats.request <= 0.0:
            sats.request = min_req_sats
        if (sats.max_cost or 0.0) < min_req_sats:
            sats.max_cost = min_req_sats

        return Model(
            id=model.id,
            name=model.name,
            created=model.created,
            description=model.description,
            context_length=model.context_length,
            architecture=model.architecture,
            pricing=model.pricing,
            sats_pricing=sats,
            per_request_limits=model.per_request_limits,
            top_provider=model.top_provider,
            enabled=model.enabled,
            upstream_provider_id=model.upstream_provider_id,
            canonical_slug=model.canonical_slug,
            alias_ids=model.alias_ids,
            forwarded_model_id=model.forwarded_model_id,
        )
    except Exception as e:
        logger.error(
            "Failed to update sats pricing for model",
            extra={
                "model_id": model.id,
                "error": str(e),
                "error_type": type(e).__name__,
            },
        )
        return model


async def _update_sats_pricing_once() -> None:
    """Update sats pricing once for all provider models (in-memory only)."""
    from ..proxy import get_upstreams, refresh_model_maps

    upstreams = get_upstreams()
    if not upstreams:
        return

    sats_to_usd = sats_usd_price()

    updated_count = 0
    for upstream in upstreams:
        updated_models = [
            _update_model_sats_pricing(m, sats_to_usd)
            for m in upstream.get_cached_models()
        ]
        upstream._models_cache = updated_models
        upstream._models_by_id = {m.forwarded_model_id or m.id: m for m in updated_models}
        updated_count += len(updated_models)

    if updated_count > 0:
        logger.info(
            f"Updated sats pricing for {updated_count} models",
            extra={"models_updated": updated_count},
        )
        await refresh_model_maps()


async def update_sats_pricing() -> None:
    """Periodically update sats pricing for all provider models and database overrides."""
    try:
        if not settings.enable_pricing_refresh:
            return
    except Exception:
        pass

    try:
        await _update_sats_pricing_once()
    except Exception as e:
        logger.warning(
            "Initial sats pricing update failed (will retry in loop)",
            extra={"error": str(e)},
        )

    while True:
        try:
            interval = getattr(settings, "pricing_refresh_interval_seconds", 120)
            jitter = max(0.0, float(interval) * 0.1)
            await asyncio.sleep(interval + random.uniform(0, jitter))
        except asyncio.CancelledError:
            break

        try:
            try:
                if not settings.enable_pricing_refresh:
                    return
            except Exception:
                pass

            await _update_sats_pricing_once()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error updating sats pricing: {e}")


class ModelTestRequest(V2BaseModel):
    model_id: str
    endpoint_type: str
    request_data: dict


@models_router.post(
    "/api/models/test", dependencies=[Depends(_require_admin_api)]
)
async def test_model(
    payload: ModelTestRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Test a model by sending a request through its configured upstream provider."""
    from sqlmodel import select

    result = await session.execute(
        select(ModelRow).where(ModelRow.id == payload.model_id)
    )
    model_row = result.scalars().first()

    if not model_row:
        return {
            "success": False,
            "error": f"Model '{payload.model_id}' not found in database",
            "status_code": 404,
        }

    provider = await session.get(UpstreamProviderRow, model_row.upstream_provider_id)
    if not provider:
        return {
            "success": False,
            "error": "Upstream provider not found",
            "status_code": 404,
        }

    endpoint_path = _MODEL_TEST_ENDPOINT_PATHS.get(payload.endpoint_type)
    if endpoint_path is None:
        raise HTTPException(status_code=400, detail="Unsupported endpoint_type")

    actual_model_id = model_row.forwarded_model_id or model_row.id
    request_data = dict(payload.request_data)
    request_data["model"] = actual_model_id

    try:
        request_size = len(json.dumps(request_data).encode("utf-8"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid request_data")
    if request_size > _MODEL_TEST_MAX_REQUEST_BYTES:
        raise HTTPException(status_code=413, detail="request_data too large")

    base_url = provider.base_url.rstrip("/")
    url = f"{base_url}/{endpoint_path}"

    logger.info(
        "admin model test",
        extra={
            "model_id": payload.model_id,
            "forwarded_model_id": actual_model_id,
            "endpoint_type": payload.endpoint_type,
            "upstream_provider_id": model_row.upstream_provider_id,
            "request_bytes": request_size,
        },
    )

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider.api_key}",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=request_data, headers=headers)
            try:
                response_data = response.json()
            except Exception:
                response_data = {"raw": response.text}

            return {
                "success": response.status_code < 400,
                "data": response_data,
                "status_code": response.status_code,
            }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "status_code": 500,
        }


@models_router.get("/v1/models")
@models_router.get("/v1/models/", include_in_schema=False)
@models_router.get("/models")
@models_router.get("/models/", include_in_schema=False)
async def models(session: AsyncSession = Depends(get_session)) -> dict:
    """Get all available models from all providers with database overrides applied."""
    from ..proxy import get_unique_models

    items = get_unique_models()
    data = []
    for model in items:
        m = model.dict()
        if model.forwarded_model_id:
            m["id"] = model.forwarded_model_id
        data.append(m)
    return {"data": data}
