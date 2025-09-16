import asyncio
import json
import random
from pathlib import Path
from urllib.request import urlopen

from fastapi import APIRouter, Depends
from pydantic.v1 import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from ..core.db import ModelRow, create_session, get_session
from ..core.logging import get_logger
from ..core.settings import settings
from .price import sats_usd_ask_price

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
                ):
                    continue

                models_data.append(model)

            return models_data
    except Exception as e:
        logger.error(f"Error fetching models from OpenRouter API: {e}")
        return []


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

    # Auto-generate models from OpenRouter API
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


def _row_to_model(row: ModelRow) -> Model:
    architecture = json.loads(row.architecture)
    pricing = json.loads(row.pricing)
    sats_pricing = json.loads(row.sats_pricing) if row.sats_pricing else None
    per_request_limits = (
        json.loads(row.per_request_limits) if row.per_request_limits else None
    )
    top_provider = json.loads(row.top_provider) if row.top_provider else None

    # Enforce minimum per-request fee on free/zero-priced models in API output
    try:
        if isinstance(pricing, dict):
            if float(pricing.get("request", 0.0)) <= 0.0:
                pricing["request"] = max(pricing.get("request", 0.0), 0.0)
        if isinstance(sats_pricing, dict):
            if float(sats_pricing.get("request", 0.0)) <= 0.0:
                # Convert min_request_msat to sats for sats_pricing fields that are in sats
                sats_min = max(1, int(settings.min_request_msat)) / 1000.0
                sats_pricing["request"] = max(
                    sats_pricing.get("request", 0.0), sats_min
                )
    except Exception:
        pass

    return Model(
        id=row.id,
        name=row.name,
        created=row.created,
        description=row.description,
        context_length=row.context_length,
        architecture=Architecture.parse_obj(architecture),
        pricing=Pricing.parse_obj(pricing),
        sats_pricing=Pricing.parse_obj(sats_pricing) if sats_pricing else None,
        per_request_limits=per_request_limits,
        top_provider=TopProvider.parse_obj(top_provider) if top_provider else None,
    )


def _model_to_row_payload(model: Model) -> dict[str, str | int | None]:
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
    }


async def list_models(session: AsyncSession | None = None) -> list[Model]:
    if session is not None:
        result = await session.exec(select(ModelRow))  # type: ignore
        rows = result.all()
        return [_row_to_model(r) for r in rows]
    async with create_session() as s:
        result = await s.exec(select(ModelRow))  # type: ignore
        rows = result.all()
        return [_row_to_model(r) for r in rows]


async def get_model_by_id(
    model_id: str, session: AsyncSession | None = None
) -> Model | None:
    if session is not None:
        row = await session.get(ModelRow, model_id)
        return _row_to_model(row) if row else None
    async with create_session() as s:
        row = await s.get(ModelRow, model_id)
        return _row_to_model(row) if row else None


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

        if not models_to_insert:
            logger.info("Bootstrapping models from OpenRouter API")
            source_filter = None
            try:
                src = settings.source or None
                source_filter = src if src and src.strip() else None
            except Exception:
                pass
            models_to_insert = fetch_openrouter_models(source_filter=source_filter)

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


async def update_sats_pricing() -> None:
    while True:
        try:
            try:
                if not settings.enable_pricing_refresh:
                    return
            except Exception:
                pass
            sats_to_usd = await sats_usd_ask_price()
            async with create_session() as s:
                result = await s.exec(select(ModelRow))  # type: ignore
                rows = result.all()
                changed = 0
                for row in rows:
                    try:
                        pricing = Pricing.parse_obj(json.loads(row.pricing))
                        top_provider = (
                            TopProvider.parse_obj(json.loads(row.top_provider))
                            if row.top_provider
                            else None
                        )
                        sats = Pricing.parse_obj(
                            {k: v / sats_to_usd for k, v in pricing.dict().items()}
                        )
                        # Enforce minimum per-request charge floor in sats
                        try:
                            min_req_msat = max(
                                1, int(getattr(settings, "min_request_msat", 1))
                            )
                        except Exception:
                            min_req_msat = 1
                        min_req_sats = float(min_req_msat) / 1000.0
                        if sats.request <= 0.0:
                            sats.request = min_req_sats
                        mspp = sats.prompt
                        mspc = sats.completion
                        if top_provider and (
                            top_provider.context_length
                            or top_provider.max_completion_tokens
                        ):
                            if (cl := top_provider.context_length) and (
                                mct := top_provider.max_completion_tokens
                            ):
                                max_prompt_cost = (cl - mct) * mspp
                                max_completion_cost = mct * mspc
                                sats.max_prompt_cost = max_prompt_cost
                                sats.max_completion_cost = max_completion_cost
                                sats.max_cost = max_prompt_cost + max_completion_cost
                            elif cl := top_provider.context_length:
                                max_prompt_cost = cl * 0.8 * mspp
                                max_completion_cost = cl * 0.2 * mspc
                                sats.max_prompt_cost = max_prompt_cost
                                sats.max_completion_cost = max_completion_cost
                                sats.max_cost = max_prompt_cost + max_completion_cost
                            elif mct := top_provider.max_completion_tokens:
                                max_prompt_cost = mct * 4 * mspp
                                max_completion_cost = mct * mspc
                                sats.max_prompt_cost = max_prompt_cost
                                sats.max_completion_cost = max_completion_cost
                                sats.max_cost = max_prompt_cost + max_completion_cost
                            else:
                                max_prompt_cost = 1_000_000 * mspp
                                max_completion_cost = 32_000 * mspc
                                sats.max_prompt_cost = max_prompt_cost
                                sats.max_completion_cost = max_completion_cost
                                sats.max_cost = max_prompt_cost + max_completion_cost
                        elif row.context_length:
                            max_prompt_cost = mspp * row.context_length * 0.8
                            max_completion_cost = mspc * row.context_length * 0.2
                            sats.max_prompt_cost = max_prompt_cost
                            sats.max_completion_cost = max_completion_cost
                            sats.max_cost = max_prompt_cost + max_completion_cost
                        else:
                            p = mspp * 1_000_000
                            c = mspc * 32_000
                            r = sats.request * 100_000
                            i = sats.image * 100
                            w = sats.web_search * 1000
                            ir = sats.internal_reasoning * 100
                            sats.max_prompt_cost = p
                            sats.max_completion_cost = c
                            sats.max_cost = p + c + r + i + w + ir

                        # Ensure overall minimum per-request total cost floor
                        if (sats.max_cost or 0.0) < min_req_sats:
                            sats.max_cost = min_req_sats

                        new_json = json.dumps(sats.dict())
                        if row.sats_pricing != new_json:
                            row.sats_pricing = new_json
                            s.add(row)
                            changed += 1
                    except Exception as per_row_error:
                        logger.error(
                            "Failed to update pricing for model",
                            extra={
                                "model_id": row.id,
                                "error": str(per_row_error),
                                "error_type": type(per_row_error).__name__,
                            },
                        )
                if changed:
                    await s.commit()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error updating sats pricing: {e}")
        try:
            interval = getattr(settings, "pricing_refresh_interval_seconds", 120)
            jitter = max(0.0, float(interval) * 0.1)
            await asyncio.sleep(interval + random.uniform(0, jitter))
        except asyncio.CancelledError:
            break


async def refresh_models_periodically() -> None:
    """Background task: periodically fetch OpenRouter models and insert new ones.

    - Respects optional SOURCE filter from settings
    - Does not overwrite existing rows
    - Sleeps according to settings.models_refresh_interval_seconds; disabled when 0
    """
    interval = getattr(settings, "models_refresh_interval_seconds", 0)
    if not interval or interval <= 0:
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
    items = await list_models(session)
    return {"data": items}
