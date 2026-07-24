import math
from typing import TYPE_CHECKING

from pydantic.v1 import BaseModel

from ..core import get_logger
from ..core.settings import settings
from .price import sats_usd_price
from .usage import normalize_usage, parse_token_count

if TYPE_CHECKING:
    from .models import Model

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


def _empty_cost(cls: type[CostData] = CostData) -> CostData:
    """Build an all-zero cost object — a full refund for an empty response.

    Shared by the two paths that must not bill: an upstream response with no
    usage data at all, and one that reports a USD cost but carries zero tokens
    in every bucket.
    """
    return cls(
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


async def calculate_cost(
    response_data: dict,
    max_cost: int,
    model_obj: "Model | None" = None,
    provider_fee: float | None = None,
) -> CostData | MaxCostData | CostDataError:
    """Calculate the cost of an API request based on token usage.

    Args:
        response_data: Response data containing usage information
        max_cost: Maximum cost in millisats
        model_obj: The model that actually served the request. When given,
            its pricing is billed directly; without it, pricing is re-derived
            from the response's model string via the alias map, which resolves
            to the best-ranked candidate — not necessarily the serving one.
        provider_fee: The serving provider's fee multiplier, applied on the
            USD-cost path and the litellm pricing fallback (configured model
            pricing already carries the fee baked in). Without it, the fee is
            re-derived from the response's model string, which yields the
            best-ranked provider's fee.

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
        return _empty_cost(MaxCostData)

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
            return _empty_cost()
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
            cost_details = usage_data.get("cost_details", {})
            if not isinstance(cost_details, dict):
                cost_details = {}
            input_usd = _coerce_usd(
                cost_details.get("input_cost")
                or cost_details.get("upstream_inference_prompt_cost")
            )
            output_usd = _coerce_usd(
                cost_details.get("output_cost")
                or cost_details.get("upstream_inference_completions_cost")
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
                provider_fee,
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
        pricing_rates = _get_pricing_rates(response_data, model_obj, provider_fee)
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

    Priority:

    1. ``cost_details.total_cost``
    2. ``cost_details.upstream_inference_cost`` (BYOK — see below)
    3. ``total_cost`` → ``cost`` (in both usage and response)

    **BYOK path (PPQ.AI):** when ``is_byok`` is true the ``usage.cost`` field
    is only a small (~5 %) routing fee, not the inference cost.  The real cost
    lives in ``cost_details.upstream_inference_cost`` and the provider's
    balance is debited by ``upstream_inference_cost + byok_fee``.  Billing just
    the fee under-charges by ~20×.
    """
    cost_details = usage_data.get("cost_details")
    if isinstance(cost_details, dict):
        cost = _coerce_usd(cost_details.get("total_cost"))
        if cost > 0:
            return cost

        # PPQ.AI BYOK: upstream_inference_cost is the real inference cost;
        # usage.cost is only a ~5 % BYOK routing fee.  Bill the sum — what PPQ
        # actually deducts from the balance.  For non-BYOK providers (e.g.
        # OpenRouter) usage.cost already equals upstream_inference_cost, so we
        # fall through to the normal ``cost`` lookup below.
        upstream_cost = _coerce_usd(
            cost_details.get("upstream_inference_cost")
        )
        if upstream_cost > 0 and usage_data.get("is_byok"):
            byok_fee = _coerce_usd(usage_data.get("cost"))
            return upstream_cost + byok_fee

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
    model_obj: "Model | None",
    provider_fee: float | None,
) -> tuple[float, float, float, float] | None:
    """Get configured rates, falling back to LiteLLM's model cost map.

    The served ``model_obj`` (when the caller has it) is billed directly;
    otherwise the response's model string is resolved through the alias map,
    which yields the best-ranked candidate rather than the serving one.

    Returns: (input_rate, output_rate, cache_read_rate, cache_write_rate).
    ``None`` means configured fixed pricing should be used by the caller.
    """
    if settings.fixed_pricing and (
        settings.fixed_per_1k_input_tokens
        or settings.fixed_per_1k_output_tokens
    ):
        return None

    from ..proxy import get_model_instance
    from .models import litellm_cost_entry

    response_model = response_data.get("model", "")
    if model_obj is None:
        logger.warning(
            "Settling without routed model identity — re-deriving pricing "
            "from the response's model string via the alias map",
            extra={"response_model": response_model},
        )
        model_obj = get_model_instance(response_model)

    if model_obj and model_obj.sats_pricing:
        try:
            mspp = float(model_obj.sats_pricing.prompt)
            mspc = float(model_obj.sats_pricing.completion)
            mscr = float(model_obj.sats_pricing.input_cache_read or 0)
            mscw = float(model_obj.sats_pricing.input_cache_write or 0)

            mspp_1k = mspp * 1_000_000.0
            mspc_1k = mspc * 1_000_000.0
            mscr_1k = mscr * 1_000_000.0 if mscr > 0 else mspp_1k
            mscw_1k = mscw * 1_000_000.0 if mscw > 0 else mspp_1k
            source = "configured"
        except Exception as e:
            logger.error("Invalid pricing data", extra={"error": str(e)})
            raise ValueError("Invalid pricing data") from e
    else:
        pricing_model = (
            model_obj.forwarded_model_id if model_obj else None
        ) or response_model
        pricing = litellm_cost_entry(pricing_model)
        if pricing is None:
            logger.error(
                "Model pricing not found in configured models or LiteLLM",
                extra={
                    "response_model": response_model,
                    "pricing_model": pricing_model,
                },
            )
            raise ValueError(f"Pricing not found for model: {response_model}")

        input_usd = _coerce_usd(pricing.get("input_cost_per_token"))
        output_usd = _coerce_usd(pricing.get("output_cost_per_token"))
        if input_usd <= 0 or output_usd <= 0:
            raise ValueError(f"Incomplete LiteLLM pricing for model: {pricing_model}")

        if provider_fee is None:
            provider_fee = _resolve_provider_fee(response_model)
        usd_per_sat = sats_usd_price()
        mspp_1k = input_usd * provider_fee * 1_000_000.0 / usd_per_sat
        mspc_1k = output_usd * provider_fee * 1_000_000.0 / usd_per_sat
        cache_read_usd = _coerce_usd(
            pricing.get("cache_read_input_token_cost")
        )
        cache_write_usd = _coerce_usd(
            pricing.get("cache_creation_input_token_cost")
        )
        mscr_1k = (
            cache_read_usd * provider_fee * 1_000_000.0 / usd_per_sat
            if cache_read_usd > 0
            else mspp_1k
        )
        mscw_1k = (
            cache_write_usd * provider_fee * 1_000_000.0 / usd_per_sat
            if cache_write_usd > 0
            else mspp_1k
        )
        source = "litellm"

    logger.info(
        "Applied model-specific pricing",
        extra={
            "model": response_model,
            "pricing_source": source,
            "input_price_msats_per_1k": mspp_1k,
            "output_price_msats_per_1k": mspc_1k,
            "cache_read_price_msats_per_1k": mscr_1k,
            "cache_write_price_msats_per_1k": mscw_1k,
        },
    )
    return mspp_1k, mspc_1k, mscr_1k, mscw_1k


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
    provider_fee: float | None,
) -> CostData:
    """Calculate cost from USD figures, deriving input/output split from tokens."""
    if provider_fee is None:
        provider_fee = _resolve_provider_fee(response_data.get("model", ""))
    usd_cost = usd_cost * provider_fee
    input_usd = input_usd * provider_fee
    output_usd = output_usd * provider_fee
    sats_per_usd = 1.0 / sats_usd_price()
    cost_in_sats = usd_cost * sats_per_usd
    cost_in_msats = math.ceil(cost_in_sats * 1000)

    if input_usd > 0 or output_usd > 0:
        # The total is the authoritative billed amount. Allocating that integer
        # total proportionally avoids losing sub-millisatoshi remainders when
        # input and output components are each truncated independently.
        component_usd = input_usd + output_usd
        input_msats = math.floor(cost_in_msats * input_usd / component_usd)
        output_msats = cost_in_msats - input_msats
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

    # Fold the cache-read/write cost into the visible ``input_msats`` so a
    # dashboard that renders I / O / T sees ``input + output == total``
    # exactly. This mirrors ``_fold_cache_into_input_tokens`` (which rolls the
    # cache token counts into the visible prompt total). The standalone
    # ``cache_read_msats`` / ``cache_creation_msats`` fields stay populated for
    # clients that want the breakdown; nothing sums the components to derive
    # ``total_msats`` (it is computed independently above), so this is
    # display-only and does not change what is billed.
    visible_output_msats = int(calc_output_msats)
    visible_input_msats = token_based_cost - visible_output_msats

    return CostData(
        base_msats=0,
        input_msats=visible_input_msats,
        output_msats=visible_output_msats,
        total_msats=token_based_cost,
        total_usd=total_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_tokens,
        cache_creation_input_tokens=cache_creation_tokens,
        cache_read_msats=int(calc_cache_read_msats),
        cache_creation_msats=int(calc_cache_write_msats),
    )
