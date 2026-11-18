"""Price multipliers keyed on what a response's usage says it was served with.

Some upstreams bill one model at several prices depending on how the request
ran, and report which one in ``usage``: Anthropic's fast mode
(``usage.speed == "fast"``) and US-only inference
(``usage.inference_geo == "us"``). The multipliers are model data, not code:
each model carries ``{usage_field: {value: multiplier}}``, filled at discovery
by a :class:`UsageMultiplierSource`, and billing multiplies every token rate by
the product of the entries the response matches. Anthropic's published prices
are a floor a source can only raise, never lower.

Request fees (``request``, ``web_search``) are not token rates and stay as is.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Protocol

from ..core import get_logger

logger = get_logger(__name__)

UsageMultipliers = dict[str, dict[str, float]]

# Usage fields a multiplier may key on. Kept explicit so an arbitrary string an
# upstream echoes in ``usage`` can never select a price.
PRICING_DIMENSIONS = ("speed", "inference_geo")

# Values that cost nothing extra. A response reporting one needs no
# multiplier, so a model without an entry for it is not a gap.
BASELINE_VALUES = {
    "speed": {"standard"},
    "inference_geo": {"global", "not_available"},
}

# Anthropic's published prices: fast mode 2x, US-only inference 1.1x. The node
# is charged at least this whenever the response reports the variant, so a
# missing, failed or lower lookup must not bill below it.
PUBLISHED_MULTIPLIERS: UsageMultipliers = {
    "speed": {"fast": 2.0},
    "inference_geo": {"us": 1.1},
}


class UsageMultiplierSource(Protocol):
    """Where model multipliers come from; swap implementations via settings."""

    async def fetch(self, model_ids: Iterable[str]) -> dict[str, UsageMultipliers]:
        """Return multipliers for the models the source knows.

        Models the source has no data for are omitted, not mapped to ``{}``,
        so callers can keep a previously known value.
        """
        ...


def pricing_dimensions(usage_data: Mapping[str, object]) -> dict[str, str]:
    """Pick the price-relevant fields a response's usage reported."""
    return {
        field: value.lower()
        for field in PRICING_DIMENSIONS
        if isinstance(value := usage_data.get(field), str) and value
    }


def usage_multiplier(
    multipliers: UsageMultipliers | None,
    dimensions: Mapping[str, str],
    model_id: str = "",
    *,
    warn: bool = True,
) -> float:
    """Product of the multipliers matching the reported dimensions.

    Each is the larger of the published floor and the model's own value.
    ``warn=False`` silences the unknown-variant warning for callers reading
    client-supplied values (the reservation) rather than upstream usage.
    """
    if not isinstance(multipliers, dict):
        multipliers = {}
    factor = 1.0
    for field, value in dimensions.items():
        if value in BASELINE_VALUES.get(field, ()):
            continue
        known = [
            rate
            for rate in (
                PUBLISHED_MULTIPLIERS.get(field, {}).get(value),
                multipliers.get(field, {}).get(value),
            )
            if rate is not None
        ]
        if not known:
            # The upstream says the request ran on a priced variant with no
            # published or sourced multiplier, so this bills at the base
            # rate. Loud, because it may under-bill.
            if warn:
                logger.warning(
                    "No usage multiplier for reported pricing dimension; billing base rate",
                    extra={"model": model_id, "field": field, "value": value},
                )
            continue
        factor *= max(known)
    return factor
