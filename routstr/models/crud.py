import json

from ..core import get_logger
from ..core.db import AsyncSession, ModelRow, UpstreamProviderRow
from ..payment.price import sats_usd_price
from .models import (
    Architecture,
    Model,
    Pricing,
    TopProvider,
    _update_model_sats_pricing,
    calculate_usd_max_costs,
)

logger = get_logger(__name__)


def row_to_model(
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
        ) = calculate_usd_max_costs(model)

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
        row_to_model(
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
    return row_to_model(row, apply_provider_fee=True, provider_fee=provider_fee)
