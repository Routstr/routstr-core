import math

from pydantic.v1 import BaseModel

from ..core import get_logger
from ..core.settings import settings
from .price import sats_usd_price
from .usage import normalize_usage, parse_token_count

__all__ = [
    "CostData",
    "CostDataError",
    "MaxCostData",
    "calculate_cost",
    "parse_token_count",
]

logger = get_logger(__name__)


class CostData(BaseModel):
    base_msats: int
    input_msats: int
    output_msats: int
    total_msats: int
    total_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_msats: int = 0
    cache_creation_msats: int = 0


class MaxCostData(CostData):
    pass


class CostDataError(BaseModel):
    message: str
    code: str


async def calculate_cost(
    response_data: dict,
    max_cost: int,
) -> CostData | MaxCostData | CostDataError:
    """Calculate the cost of an API request based on token usage.

    Args:
        response_data: Response data containing usage information
        max_cost: Maximum cost in millisats

    Returns:
        Cost data or error information

    The response's usage object is normalized with the default union parser;
    this function holds no vendor-dialect knowledge of its own.
    """
    logger.debug(
        "Starting cost calculation",
        extra={
            "max_cost_msats": max_cost,
            "has_usage_data": "usage" in response_data,
            "response_model": response_data.get("model", "unknown"),
        },
    )

    usage = normalize_usage(response_data.get("usage"))

    if usage is None:
        logger.warning(
            "No usage data in response — billing at MaxCostData with zero "
            "tokens. Dashboard will show this request as `(0+0)`. Most "
            "common cause: upstream stream did not include a final usage "
            "chunk (OpenAI-compat backends require "
            "`stream_options.include_usage=true`).",
            extra={
                "max_cost_msats": max_cost,
                "model": response_data.get("model", "unknown"),
                "response_keys": sorted(response_data.keys())
                if isinstance(response_data, dict)
                else None,
            },
        )
        return MaxCostData(
            base_msats=0,
            input_msats=0,
            output_msats=0,
            total_msats=0,
            total_usd=0.0,
            input_tokens=0,
            output_tokens=0,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_msats=0,
            cache_creation_msats=0,
        )

    usage_data = response_data.get("usage") or {}
    if not isinstance(usage_data, dict):
        usage_data = {}

    input_tokens = usage.input_tokens
    output_tokens = usage.output_tokens
    cache_read_tokens = usage.cache_read_tokens
    cache_creation_tokens = usage.cache_write_tokens

    # Try USD cost first
    usd_cost = _resolve_usd_cost(usage_data, response_data)
    if usd_cost > 0:
        truly_empty = (
            input_tokens == 0
            and output_tokens == 0
            and cache_read_tokens == 0
            and cache_creation_tokens == 0
        )
        if truly_empty:
            logger.warning(
                "Upstream reported a USD cost but the response carries no "
                "tokens at all (input, output, cache-read and cache-creation "
                "are all zero) — refunding in full rather than billing the "
                "USD-derived cost for an empty response.",
                extra={
                    "model": response_data.get("model", "unknown"),
                    "usd_cost": usd_cost,
                    "usage_keys": sorted(usage_data.keys())
                    if isinstance(usage_data, dict)
                    else None,
                },
            )
            return CostData(
                base_msats=0,
                input_msats=0,
                output_msats=0,
                total_msats=0,
                total_usd=0.0,
                input_tokens=0,
                output_tokens=0,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
                cache_read_msats=0,
                cache_creation_msats=0,
            )
        if input_tokens == 0 and output_tokens == 0:
            logger.warning(
                "Upstream reported a USD cost but no token counts — "
                "billing the USD-derived cost while the dashboard will "
                "show this request as `(0+0)` tokens. Check that the "
                "upstream actually emits `usage.input_tokens` and "
                "`usage.output_tokens` (OpenAI-compat streams require "
                "`stream_options.include_usage=true`).",
                extra={
                    "model": response_data.get("model", "unknown"),
                    "usd_cost": usd_cost,
                    "usage_keys": sorted(usage_data.keys())
                    if isinstance(usage_data, dict)
                    else None,
                },
            )
        try:
            input_usd = _coerce_usd(
                usage_data.get("cost_details", {}).get("input_cost", 0)
            )
            output_usd = _coerce_usd(
                usage_data.get("cost_details", {}).get("output_cost", 0)
            )
            return _calculate_from_usd_cost(
                usd_cost,
                input_usd,
                output_usd,
                input_tokens,
                cache_read_tokens,
                cache_creation_tokens,
                output_tokens,
                response_data,
            )
        except Exception as e:
            logger.warning(
                "Error calculating cost from usage data",
                extra={
                    "error": str(e),
                    "usd_cost": usd_cost,
                    "model": response_data.get("model", "unknown"),
                },
            )

    # Fall back to token-based pricing
    try:
        pricing_rates = _get_pricing_rates(response_data)
    except ValueError as e:
        return CostDataError(message=str(e), code="pricing_error")

    if pricing_rates is None:
        input_rate = float(settings.fixed_per_1k_input_tokens) * 1000.0
        output_rate = float(settings.fixed_per_1k_output_tokens) * 1000.0
        cache_read_rate = input_rate
        cache_creation_rate = input_rate
    else:
        input_rate, output_rate, cache_read_rate, cache_creation_rate = pricing_rates

    if not (input_rate and output_rate):
        logger.warning(
            "No token pricing configured — billing at flat MaxCostData. "
            "Token counts %s in the upstream response but cannot be "
            "priced; the request will appear in dashboards with the "
            "raw counts and a fixed max-cost charge.",
            "are present"
            if (input_tokens > 0 or output_tokens > 0)
            else "are zero",
            extra={
                "base_cost_msats": max_cost,
                "model": response_data.get("model", "unknown"),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        )
        return MaxCostData(
            base_msats=max_cost,
            input_msats=0,
            output_msats=0,
            total_msats=max_cost,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read_tokens,
            cache_creation_input_tokens=cache_creation_tokens,
            cache_read_msats=0,
            cache_creation_msats=0,
        )

    return _calculate_from_tokens(
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_creation_tokens,
        input_rate,
        output_rate,
        cache_read_rate,
        cache_creation_rate,
        response_data,
    )


# ============================================================================
# Helper Functions (ordered by call sequence in calculate_cost)
# ============================================================================


def _coerce_usd(value: object) -> float:
    """Coerce a value to USD float, handling various formats safely."""
    if value is None or isinstance(value, bool):
        return 0.0
    if not isinstance(value, (int, float, str)):
        return 0.0
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _resolve_usd_cost(usage_data: dict, response_data: dict) -> float:
    """Resolve USD cost with clear priority order.

    Priority: cost_details.total_cost → total_cost → cost (in both usage and response).
    """
    cost_details = usage_data.get("cost_details")
    if isinstance(cost_details, dict):
        cost = _coerce_usd(cost_details.get("total_cost"))
        if cost > 0:
            return cost

    for source in [usage_data, response_data]:
        if not isinstance(source, dict):
            continue
        for field in ("total_cost", "cost"):
            cost = _coerce_usd(source.get(field))
            if cost > 0:
                return cost

    return 0.0


def _get_pricing_rates(
    response_data: dict,
) -> tuple[float, float, float, float] | None:
    """Get model-based pricing rates or None if using fixed pricing.

    Returns: (input_rate, output_rate, cache_read_rate, cache_write_rate)
    """
    if settings.fixed_pricing:
        return None

    from ..proxy import get_model_instance

    response_model = response_data.get("model", "")
    model_obj = get_model_instance(response_model)

    if not model_obj:
        logger.error("Invalid model in response", extra={"response_model": response_model})
        raise ValueError(f"Invalid model: {response_model}")

    if not model_obj.sats_pricing:
        logger.error(
            "Model pricing not defined",
            extra={"model": response_model, "model_id": response_model},
        )
        raise ValueError("Model pricing not defined")

    try:
        mspp = float(model_obj.sats_pricing.prompt)
        mspc = float(model_obj.sats_pricing.completion)
        mscr = float(model_obj.sats_pricing.input_cache_read or 0)
        mscw = float(model_obj.sats_pricing.input_cache_write or 0)

        mspp_1k = mspp * 1_000_000.0
        mspc_1k = mspc * 1_000_000.0
        mscr_1k = mscr * 1_000_000.0 if mscr > 0 else mspp_1k
        mscw_1k = mscw * 1_000_000.0 if mscw > 0 else mspp_1k

        logger.info(
            "Applied model-specific pricing",
            extra={
                "model": response_model,
                "input_price_msats_per_1k": mspp_1k,
                "output_price_msats_per_1k": mspc_1k,
                "cache_read_price_msats_per_1k": mscr_1k,
                "cache_write_price_msats_per_1k": mscw_1k,
            },
        )
        return mspp_1k, mspc_1k, mscr_1k, mscw_1k
    except Exception as e:
        logger.error("Invalid pricing data", extra={"error": str(e)})
        raise ValueError("Invalid pricing data") from e


def _resolve_provider_fee(model_id: str) -> float:
    """Resolve the provider fee multiplier for the given model id.

    Falls back to 1.0 (no markup) when the provider cannot be resolved so
    the USD cost path never silently double-applies or omits the fee.
    """
    from ..proxy import get_provider_for_model

    if not model_id:
        return 1.0
    providers = get_provider_for_model(model_id)
    if not providers:
        return 1.0
    return float(providers[0].provider_fee)


def _calculate_from_usd_cost(
    usd_cost: float,
    input_usd: float,
    output_usd: float,
    input_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
    output_tokens: int,
    response_data: dict,
) -> CostData:
    """Calculate cost from USD figures, deriving input/output split from tokens."""
    provider_fee = _resolve_provider_fee(response_data.get("model", ""))
    usd_cost = usd_cost * provider_fee
    input_usd = input_usd * provider_fee
    output_usd = output_usd * provider_fee
    sats_per_usd = 1.0 / sats_usd_price()
    cost_in_sats = usd_cost * sats_per_usd
    cost_in_msats = math.ceil(cost_in_sats * 1000)

    if input_usd > 0 or output_usd > 0:
        input_msats = int((input_usd * sats_per_usd) * 1000)
        output_msats = int((output_usd * sats_per_usd) * 1000)
    else:
        effective_input_tokens = (
            input_tokens + cache_read_tokens + cache_creation_tokens
        )
        total_tokens = effective_input_tokens + output_tokens
        input_msats = (
            int(cost_in_msats * effective_input_tokens / total_tokens)
            if total_tokens > 0
            else 0
        )
        output_msats = cost_in_msats - input_msats

    logger.info(
        "Using cost from usage data/details",
        extra={
            "usd_cost": usd_cost,
            "cost_in_sats": cost_in_sats,
            "cost_in_msats": cost_in_msats,
            "model": response_data.get("model", "unknown"),
        },
    )

    return CostData(
        base_msats=0,
        input_msats=input_msats,
        output_msats=output_msats,
        total_msats=cost_in_msats,
        total_usd=usd_cost,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_tokens,
        cache_creation_input_tokens=cache_creation_tokens,
        cache_read_msats=0,
        cache_creation_msats=0,
    )


def _calculate_from_tokens(
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
    input_rate: float,
    output_rate: float,
    cache_read_rate: float,
    cache_creation_rate: float,
    response_data: dict,
) -> CostData:
    """Calculate cost from token counts using pricing rates."""
    calc_input_msats = round(input_tokens / 1000 * input_rate, 3)
    calc_output_msats = round(output_tokens / 1000 * output_rate, 3)
    calc_cache_read_msats = round(cache_read_tokens / 1000 * cache_read_rate, 3)
    calc_cache_write_msats = round(
        cache_creation_tokens / 1000 * cache_creation_rate, 3
    )
    token_based_cost = math.ceil(
        calc_input_msats
        + calc_output_msats
        + calc_cache_read_msats
        + calc_cache_write_msats
    )
    total_usd = (token_based_cost / 1000.0) * sats_usd_price()

    logger.info(
        "Calculated token-based cost",
        extra={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read_tokens,
            "cache_creation_input_tokens": cache_creation_tokens,
            "input_cost_msats": calc_input_msats,
            "output_cost_msats": calc_output_msats,
            "cache_read_cost_msats": calc_cache_read_msats,
            "cache_creation_cost_msats": calc_cache_write_msats,
            "total_cost_msats": token_based_cost,
            "total_usd": total_usd,
            "model": response_data.get("model", "unknown"),
        },
    )

    return CostData(
        base_msats=0,
        input_msats=int(calc_input_msats),
        output_msats=int(calc_output_msats),
        total_msats=token_based_cost,
        total_usd=total_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_tokens,
        cache_creation_input_tokens=cache_creation_tokens,
        cache_read_msats=int(calc_cache_read_msats),
        cache_creation_msats=int(calc_cache_write_msats),
    )
