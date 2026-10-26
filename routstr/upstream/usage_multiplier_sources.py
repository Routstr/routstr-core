"""Implementations of :class:`UsageMultiplierSource`, selected by settings.

Only OpenRouter exists today. Another source (litellm's cost map, a provider's
own price list, a static table) plugs in by implementing ``fetch`` and adding a
name to ``_SOURCES``; nothing on the billing side changes.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from urllib.parse import quote

import httpx

from ..core import get_logger
from ..core.settings import settings
from ..payment.rates import coerce_rate
from ..payment.usage_multipliers import UsageMultipliers, UsageMultiplierSource

logger = get_logger(__name__)

OPENROUTER_API_URL = "https://openrouter.ai/api/v1"
ENDPOINTS_TIMEOUT_SECONDS = 10.0
ENDPOINTS_CONCURRENCY = 8
# Models refresh every few minutes; endpoint prices change far less often.
ENDPOINTS_CACHE_TTL_SECONDS = 3600.0
# Prompt and completion ratios of one endpoint pair must agree this closely,
# and land in this range, before the pair is trusted as a flat multiplier.
RATIO_TOLERANCE = 0.01
RATIO_BOUNDS = (1.0, 10.0)


def _ratio(priced: dict[str, float], base: dict[str, float]) -> float | None:
    """One multiplier explaining both token rates of a pair, else ``None``."""
    ratios = [priced[key] / base[key] for key in ("prompt", "completion")]
    low, high = min(ratios), max(ratios)
    if high - low > RATIO_TOLERANCE * low:
        return None
    if not RATIO_BOUNDS[0] <= low <= RATIO_BOUNDS[1]:
        return None
    return round((low + high) / 2, 3)


def _agreed(ratios: list[float]) -> float | None:
    """The shared value of several ratios, or ``None`` if they disagree."""
    if not ratios or max(ratios) - min(ratios) > RATIO_TOLERANCE * min(ratios):
        return None
    return round(sum(ratios) / len(ratios), 3)


def derive_multipliers(endpoints: list[dict], provider_tag: str) -> UsageMultipliers:
    """Read multipliers off OpenRouter's per-endpoint prices for one model.

    OpenRouter lists every priced variant of a model as its own endpoint:
    ``anthropic/fast`` is Anthropic's fast mode, ``azure/us`` or
    ``amazon-bedrock/us-east-1`` a US-pinned deployment. Each variant's price
    divided by its unpinned sibling's is the multiplier.

    - ``speed.fast`` comes only from ``<provider_tag>/fast``: fast mode is the
      upstream's own feature, and its absence means the model does not have it.
    - ``inference_geo.us`` comes from the provider's own ``/us`` endpoint when
      OpenRouter lists one. Anthropic's first-party API has none there, so the
      US-pinned deployments of other hosts stand in, but only when every one
      of them agrees on the same ratio.
    """
    prices: dict[str, dict[str, float]] = {}
    for endpoint in endpoints:
        tag = endpoint.get("tag")
        pricing = endpoint.get("pricing") or {}
        prompt = coerce_rate(pricing.get("prompt"))
        completion = coerce_rate(pricing.get("completion"))
        if isinstance(tag, str) and prompt and completion:
            prices[tag] = {"prompt": prompt, "completion": completion}

    multipliers: UsageMultipliers = {}

    base = prices.get(provider_tag)
    fast = prices.get(f"{provider_tag}/fast")
    if base and fast and (rate := _ratio(fast, base)) is not None:
        multipliers["speed"] = {"fast": rate}

    own: list[float] = []
    others: list[float] = []
    for tag, priced in prices.items():
        family, _, region = tag.partition("/")
        if region != "us" and not region.startswith("us-"):
            continue
        reference = prices.get(family) or prices.get(f"{family}/global")
        if reference is None or (rate := _ratio(priced, reference)) is None:
            continue
        (own if family == provider_tag else others).append(rate)
    if (rate := _agreed(own) or _agreed(others)) is not None:
        multipliers["inference_geo"] = {"us": rate}

    return multipliers


class OpenRouterEndpointsSource:
    """Multipliers from ``GET /models/{id}/endpoints`` on OpenRouter."""

    def __init__(
        self,
        provider_tag: str,
        *,
        base_url: str = OPENROUTER_API_URL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.provider_tag = provider_tag
        self.base_url = base_url.rstrip("/")
        self._clock = clock
        self._cache: dict[str, tuple[float, UsageMultipliers]] = {}

    async def fetch(self, model_ids: Iterable[str]) -> dict[str, UsageMultipliers]:
        now = self._clock()
        result: dict[str, UsageMultipliers] = {}
        stale: list[str] = []
        for model_id in dict.fromkeys(model_ids):
            cached = self._cache.get(model_id)
            if cached and now - cached[0] < ENDPOINTS_CACHE_TTL_SECONDS:
                result[model_id] = cached[1]
            else:
                stale.append(model_id)
        if not stale:
            return result

        semaphore = asyncio.Semaphore(ENDPOINTS_CONCURRENCY)
        async with httpx.AsyncClient(timeout=ENDPOINTS_TIMEOUT_SECONDS) as client:

            async def one(model_id: str) -> tuple[str, UsageMultipliers | None]:
                async with semaphore:
                    try:
                        response = await client.get(
                            f"{self.base_url}/models/{quote(model_id, safe='/')}/endpoints"
                        )
                        response.raise_for_status()
                        endpoints = response.json()["data"]["endpoints"]
                        if not isinstance(endpoints, list):
                            raise ValueError("endpoints is not a list")
                    except Exception as e:
                        # Omitted, so the caller keeps whatever it last knew.
                        logger.warning(
                            "Could not fetch OpenRouter endpoints for usage multipliers",
                            extra={"model": model_id, "error": repr(e)},
                        )
                        return model_id, None
                    return model_id, derive_multipliers(endpoints, self.provider_tag)

            for model_id, multipliers in await asyncio.gather(
                *(one(model_id) for model_id in stale)
            ):
                if multipliers is not None:
                    self._cache[model_id] = (now, multipliers)
                    result[model_id] = multipliers
        return result


_SOURCES: dict[str, Callable[[str], UsageMultiplierSource]] = {
    "openrouter": OpenRouterEndpointsSource,
}
_instances: dict[tuple[str, str], UsageMultiplierSource] = {}


def get_usage_multiplier_source(provider_tag: str) -> UsageMultiplierSource | None:
    """The configured source for one upstream family, or ``None`` if disabled."""
    name = settings.usage_multiplier_source
    factory = _SOURCES.get(name)
    if factory is None:
        if name != "none":
            logger.warning("Unknown usage multiplier source", extra={"source": name})
        return None
    key = (name, provider_tag)
    if key not in _instances:
        _instances[key] = factory(provider_tag)
    return _instances[key]
