import json
from pathlib import Path

from ..core import get_logger
from ..core.db import AsyncSession, ModelRow, UpstreamProviderRow
from ..core.settings import settings
from ..payment.price import sats_usd_price
from .metadata import fetch_openrouter_models
from .models import (
    Architecture,
    Model,
    Pricing,
    TopProvider,
    _calculate_usd_max_costs,
    _update_model_sats_pricing,
    is_openrouter_upstream,
)

logger = get_logger(__name__)


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
    row = await session.get(ModelRow, (model_id, provider_id))
    if not row or not row.enabled:
        return None
    provider = await session.get(UpstreamProviderRow, provider_id)
    provider_fee = provider.provider_fee if provider else 1.01
    return _row_to_model(row, apply_provider_fee=True, provider_fee=provider_fee)
