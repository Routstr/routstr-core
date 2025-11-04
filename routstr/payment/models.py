import asyncio
import json
import random
from pathlib import Path
from urllib.request import urlopen

import httpx
from fastapi import APIRouter, Depends
from pydantic.v1 import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from ..core.db import ModelRow, create_session, get_session
from ..core.logging import get_logger
from ..core.settings import settings
from .price import sats_usd_price

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

    def __hash__(self) -> int:
        return hash(self.id)


def fetch_openrouter_models(source_filter: str | None = None) -> list[dict]:
    """Fetches model information from OpenRouter API."""
    base_url = "https://openrouter.ai/api/v1"

    try:
        with urlopen(f"{base_url}/models") as response:
            data = json.loads(response.read().decode("utf-8"))

            models_data: list[dict] = []
            for model in data.get("data", []):
                model_id = model.get("id", "")

                if source_filter:
                    source_prefix = f"{source_filter}/"
                    if not model_id.startswith(source_prefix):
                        continue

                    model = dict(model)
                    model["id"] = model_id[len(source_prefix) :]
                    model_id = model["id"]

                if (
                    "(free)" in model.get("name", "")
                    or model_id == "openrouter/auto"
                    or model_id == "google/gemini-2.5-pro-exp-03-25"
                    or model_id == "opengvlab/internvl3-78b"
                    or model_id == "openrouter/sonoma-dusk-alpha"
                    or model_id == "openrouter/sonoma-sky-alpha"
                ):
                    continue

                models_data.append(model)

            return models_data
    except Exception as e:
        logger.error(f"Error fetching models from OpenRouter API: {e}")
        return []


async def async_fetch_openrouter_models(source_filter: str | None = None) -> list[dict]:
    """Asynchronously fetch model information from OpenRouter API."""
    base_url = "https://openrouter.ai/api/v1"

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{base_url}/models", timeout=30)
            response.raise_for_status()
            data = response.json()

            models_data: list[dict] = []
            for model in data.get("data", []):
                model_id = model.get("id", "")

                if source_filter:
                    source_prefix = f"{source_filter}/"
                    if not model_id.startswith(source_prefix):
                        continue

                    model = dict(model)
                    model["id"] = model_id[len(source_prefix) :]
                    model_id = model["id"]

                if (
                    "(free)" in model.get("name", "")
                    or model_id == "openrouter/auto"
                    or model_id == "google/gemini-2.5-pro-exp-03-25"
                    or model_id == "opengvlab/internvl3-78b"
                    or model_id == "openrouter/sonoma-dusk-alpha"
                    or model_id == "openrouter/sonoma-sky-alpha"
                ):
                    continue

                models_data.append(model)

            return models_data
    except Exception as e:
        logger.error(f"Error (async) fetching models from OpenRouter API: {e}")
        return []


def is_openrouter_upstream() -> bool:
    try:
        base = (settings.upstream_base_url or "").strip().rstrip("/")
    except Exception:
        return False
    return base.lower() == "https://openrouter.ai/api/v1"


def load_models() -> list[Model]:
    """Load model definitions from a JSON file or auto-generate from OpenRouter API.

    The file path can be specified via the ``MODELS_PATH`` environment variable.
    If a user-provided models.json exists, it will be used. Otherwise, models are
    automatically fetched from OpenRouter API in memory. If the example file exists
    and no user file is provided, it will be used as a fallback.
    """

    try:
        models_path = Path(settings.models_path)
    except Exception:
        models_path = Path("models.json")

    # Check if user has actively provided a models.json file
    if models_path.exists():
        logger.info(f"Loading models from user-provided file: {models_path}")
        try:
            with models_path.open("r") as f:
                data = json.load(f)
            return [Model(**model) for model in data.get("models", [])]  # type: ignore
        except Exception as e:
            logger.error(f"Error loading models from {models_path}: {e}")
            # Fall through to auto-generation

    # Only auto-generate from OpenRouter when upstream is OpenRouter
    if not is_openrouter_upstream():
        logger.info(
            "Skipping auto-generation from OpenRouter because upstream_base_url is not https://openrouter.ai/api/v1"
        )
        return []

    logger.info("Auto-generating models from OpenRouter API")
    try:
        source_filter = settings.source or None
    except Exception:
        source_filter = None
    source_filter = source_filter if source_filter and source_filter.strip() else None

    models_data = fetch_openrouter_models(source_filter=source_filter)
    if not models_data:
        logger.error("Failed to fetch models from OpenRouter API")
        return []

    logger.info(f"Successfully fetched {len(models_data)} models from OpenRouter API")
    return [Model(**model) for model in models_data]  # type: ignore


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


async def list_models(
    session: AsyncSession,
    upstream_id: int,
    include_disabled: bool = False,
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
            apply_provider_fee=True,
            provider_fee=providers_by_id[r.upstream_provider_id].provider_fee
            if r.upstream_provider_id in providers_by_id
            else 1.01,
        )
        for r in rows
    ]


async def get_model_by_id(
    model_id: str, provider_id: int, session: AsyncSession
) -> Model | None:
    from ..core.db import UpstreamProviderRow

    row = await session.get(ModelRow, (model_id, provider_id))
    if not row or not row.enabled:
        return None
    provider = await session.get(UpstreamProviderRow, provider_id)
    provider_fee = provider.provider_fee if provider else 1.01
    return _row_to_model(row, apply_provider_fee=True, provider_fee=provider_fee)


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
            return (
                (cl - mct) * prompt_price,
                mct * completion_price,
                (cl - mct) * prompt_price + mct * completion_price,
            )
        elif cl := model.top_provider.context_length:
            return (
                cl * 0.8 * prompt_price,
                cl * 0.2 * completion_price,
                cl * prompt_price,
            )
        elif mct := model.top_provider.max_completion_tokens:
            return (
                mct * 4 * prompt_price,
                mct * completion_price,
                mct * 5 * prompt_price,
            )
    elif model.context_length:
        return (
            model.context_length * 0.8 * prompt_price,
            model.context_length * 0.2 * completion_price,
            model.context_length * prompt_price,
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


async def ensure_models_bootstrapped() -> None:
    async with create_session() as s:
        existing = (await s.exec(select(ModelRow.id).limit(1))).all()  # type: ignore
        if existing:
            return

        try:
            models_path = Path(settings.models_path)
        except Exception:
            models_path = Path("models.json")

        models_to_insert: list[dict] = []
        if models_path.exists():
            try:
                with models_path.open("r") as f:
                    data = json.load(f)
                models_to_insert = data.get("models", [])
                logger.info(
                    f"Bootstrapping {len(models_to_insert)} models from {models_path}"
                )
            except Exception as e:
                logger.error(f"Error loading models from {models_path}: {e}")

        if not models_to_insert and is_openrouter_upstream():
            logger.info("Bootstrapping models from OpenRouter API")
            source_filter = None
            try:
                src = settings.source or None
                source_filter = src if src and src.strip() else None
            except Exception:
                pass
            models_to_insert = fetch_openrouter_models(source_filter=source_filter)
        elif not models_to_insert:
            logger.info(
                "No models.json found and upstream is not OpenRouter; skipping bootstrap"
            )

        for m in models_to_insert:
            try:
                model = Model(**m)  # type: ignore
            except Exception:
                # Some OpenRouter models include extra fields; only map required ones
                continue
            exists = await s.get(ModelRow, model.id)
            if exists:
                continue
            payload = _model_to_row_payload(model)
            s.add(ModelRow(**payload))  # type: ignore
        await s.commit()


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
    db_pricing: dict, upstream_pricing: dict, tolerance: float = 0.1
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
        db_val = float(db_pricing.get(key, 0.0)) * 1000000
        upstream_val = float(upstream_pricing.get(key, 0.0)) * 1000000

        if abs(db_val - upstream_val) > tolerance:
            return False

    return True


async def refresh_models_periodically() -> None:
    """Background task: periodically fetch OpenRouter models and insert new ones.

    - Respects optional SOURCE filter from settings
    - Does not overwrite existing rows
    - Sleeps according to settings.models_refresh_interval_seconds; disabled when 0
    """
    interval = getattr(settings, "models_refresh_interval_seconds", 0)
    if not interval or interval <= 0:
        return

    # Only refresh from OpenRouter when upstream is OpenRouter
    if not is_openrouter_upstream():
        logger.info("Skipping models refresh: upstream_base_url is not OpenRouter")
        return

    while True:
        try:
            try:
                if not settings.enable_models_refresh:
                    return
            except Exception:
                pass
            try:
                src = settings.source or None
                source_filter = src if src and src.strip() else None
            except Exception:
                source_filter = None

            models = fetch_openrouter_models(source_filter=source_filter)
            if not models:
                await asyncio.sleep(interval)
                continue

            async with create_session() as s:
                result = await s.exec(select(ModelRow.id))  # type: ignore
                existing_ids = {
                    row[0] if isinstance(row, tuple) else row for row in result.all()
                }
                inserted = 0
                for m in models:
                    try:
                        model = Model(**m)  # type: ignore
                    except Exception:
                        continue
                    if model.id in existing_ids:
                        continue
                    payload = _model_to_row_payload(model)
                    try:
                        s.add(ModelRow(**payload))  # type: ignore
                    except Exception:
                        pass
                    inserted += 1
                if inserted:
                    await s.commit()
                    logger.info(f"Inserted {inserted} new models from OpenRouter")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(
                "Error during models refresh",
                extra={"error": str(e), "error_type": type(e).__name__},
            )
        try:
            jitter = max(0.0, float(interval) * 0.1)
            await asyncio.sleep(interval + random.uniform(0, jitter))
        except asyncio.CancelledError:
            break


@models_router.get("/v1/models")
@models_router.get("/models", include_in_schema=False)
async def models(session: AsyncSession = Depends(get_session)) -> dict:
    """Get all available models from all providers with database overrides applied."""
    from ..proxy import get_unique_models

    items = get_unique_models()
    return {"data": items}
