import asyncio
import json
import random

from fastapi import APIRouter, Depends
from pydantic.v1 import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from ..core.db import ModelRow, create_session, get_session
from ..core.logging import get_logger
from ..core.settings import settings
from ..payment.price import sats_usd_price

logger = get_logger(__name__)

models_router = APIRouter()


class Architecture(BaseModel):
    modality: str
    input_modalities: list[str]
    output_modalities: list[str]
    tokenizer: str
    instruct_type: str | None


class Pricing(BaseModel):
    prompt: float
    completion: float
    request: float
    image: float
    web_search: float
    internal_reasoning: float
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
    upstream_provider_id: int | None = None
    canonical_slug: str | None = None
    alias_ids: list[str] | None = None

    def __hash__(self) -> int:
        return hash(self.id)


def is_openrouter_upstream() -> bool:
    try:
        base = (settings.upstream_base_url or "").strip().rstrip("/")
    except Exception:
        return False
    return base.lower() == "https://openrouter.ai/api/v1"


def calculate_usd_max_costs(model: Model) -> tuple[float, float, float]:
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
    from ..proxy import get_upstreams

    upstreams = get_upstreams()
    sats_to_usd = sats_usd_price()

    updated_count = 0
    for upstream in upstreams:
        updated_models = [
            _update_model_sats_pricing(m, sats_to_usd)
            for m in upstream.get_cached_models()
        ]
        upstream._models_cache = updated_models
        upstream._models_by_id = {m.id: m for m in updated_models}
        updated_count += len(updated_models)

    if updated_count > 0:
        logger.info("Updated sats pricing", extra={"models_updated": updated_count})


async def update_sats_pricing() -> None:
    """Periodically update sats pricing for all provider models and database overrides."""
    try:
        if not settings.enable_pricing_refresh:
            return
    except Exception:
        pass

    await _update_sats_pricing_once()

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


async def cleanup_enabled_models_periodically() -> None:
    """Background task to clean up enabled models that match upstream pricing.

    When model is enabled (enabled=True), remove it from DB if it matches upstream pricing.
    Keep it in DB only if pricing differs from upstream or if it's disabled.
    """
    interval = getattr(
        settings, "models_cleanup_interval_seconds", 300
    )  # 5 minutes default
    if not interval or interval <= 0:
        return

    while True:
        try:
            await _cleanup_enabled_models_once()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(
                "Error during enabled models cleanup",
                extra={"error": str(e), "error_type": type(e).__name__},
            )

        try:
            jitter = max(0.0, float(interval) * 0.1)
            await asyncio.sleep(interval + random.uniform(0, jitter))
        except asyncio.CancelledError:
            break


async def _cleanup_enabled_models_once() -> None:
    """Clean up enabled models that match upstream pricing."""
    from ..proxy import get_upstreams

    async with create_session() as session:
        # Get all enabled models from DB
        result = await session.exec(
            select(ModelRow).where(
                ModelRow.enabled,  # Only enabled models
            )
        )
        db_models = result.all()

        if not db_models:
            return

        upstreams = get_upstreams()
        models_to_remove = []

        for db_model in db_models:
            # Find corresponding upstream model
            upstream_model = None
            for upstream in upstreams:
                upstream_model = upstream.get_cached_model_by_id(db_model.id)
                if upstream_model:
                    break

            if not upstream_model:
                continue

            # Compare pricing to see if they match
            db_pricing = json.loads(db_model.pricing)
            upstream_pricing = upstream_model.pricing.dict()

            # Check if pricing matches (with small tolerance for float comparison)
            pricing_matches = _pricing_matches(db_pricing, upstream_pricing)

            if pricing_matches:
                models_to_remove.append(db_model)
                logger.info(
                    f"Removing enabled model {db_model.id} - matches upstream pricing",
                    extra={"model_id": db_model.id},
                )

        # Remove models that match upstream pricing
        for model in models_to_remove:
            await session.delete(model)

        if models_to_remove:
            await session.commit()
            logger.info(
                f"Cleaned up {len(models_to_remove)} enabled models that match upstream pricing"
            )


def _pricing_matches(
    db_pricing: dict, upstream_pricing: dict, tolerance: float = 0.0
) -> bool:
    """Check if pricing dictionaries match within tolerance."""
    keys_to_compare = [
        "prompt",
        "completion",
        "request",
        "image",
        "web_search",
        "internal_reasoning",
    ]

    for key in keys_to_compare:
        db_val = int(float(db_pricing.get(key, 0.0)) * 1000000)
        upstream_val = int(float(upstream_pricing.get(key, 0.0)) * 1000000)

        if abs(db_val - upstream_val) > tolerance:
            return False

    return True


def _model_to_row_payload(model: Model) -> dict[str, str | int | bool | None]:
    return {
        "id": model.id,
        "name": model.name,
        "created": model.created,
        "description": model.description,
        "context_length": model.context_length,
        "architecture": json.dumps(model.architecture.dict()),
        "pricing": json.dumps(model.pricing.dict()),
        "sats_pricing": json.dumps(model.sats_pricing.dict())
        if model.sats_pricing
        else None,
        "per_request_limits": json.dumps(model.per_request_limits)
        if model.per_request_limits is not None
        else None,
        "top_provider": json.dumps(model.top_provider.dict())
        if model.top_provider is not None
        else None,
        "enabled": model.enabled,
        "upstream_provider_id": model.upstream_provider_id,
    }


@models_router.get("/v1/models")
@models_router.get("/models", include_in_schema=False)
async def models(session: AsyncSession = Depends(get_session)) -> dict:
    """Get all available models from all providers with database overrides applied."""
    from ..proxy import get_unique_models

    items = get_unique_models()
    return {"data": items}
