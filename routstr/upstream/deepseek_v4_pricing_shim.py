"""TEMPORARY: local DeepSeek V4 pricing shim.

litellm's bundled cost map does not yet ship ``deepseek-v4-flash`` /
``deepseek-v4-pro``. Without an entry, ``backfill_cache_pricing`` cannot find a
``cache_read_input_token_cost`` and cache reads fall back to the full input
rate — a large overcharge on cache hits (DeepSeek V4 hits are ~0.008-0.02x
input, i.e. cached tokens cost 50-120x less than regular input).

This module injects the missing entries into ``litellm.model_cost`` at startup
so the existing backfill path resolves them. Rates mirror the canonical
``deepseek`` provider entries now in litellm's ``model_prices`` map
(``input_cost_per_token`` is the cache-*miss* rate;
``cache_read_input_token_cost`` is the cache-*hit* rate), sourced from
https://api-docs.deepseek.com/quick_start/pricing via
https://github.com/BerriAI/litellm/pull/26380 (issue
https://github.com/BerriAI/litellm/issues/30430).

=== REMOVAL (once litellm ships these models) ===
Delete this file and the single ``register_deepseek_v4_pricing()`` call in
``routstr/core/main.py``. Nothing else depends on it. Entries are only added
when absent, so a stale shim is harmless after upstream lands — but remove it.
"""

import litellm

from ..core import get_logger

logger = get_logger(__name__)

# USD per token. Mirrors the canonical ``deepseek`` provider entries in
# litellm's model_prices map (source: DeepSeek API pricing docs). Keep these in
# sync with ``litellm.model_cost["deepseek/deepseek-v4-*"]``.
_DEEPSEEK_V4_RATES: dict[str, dict[str, float]] = {
    "deepseek-v4-flash": {
        "input_cost_per_token": 1.4e-07,
        "output_cost_per_token": 2.8e-07,
        "cache_read_input_token_cost": 2.8e-09,
        "cache_creation_input_token_cost": 0.0,
        "input_cost_per_token_cache_hit": 2.8e-09,
    },
    "deepseek-v4-pro": {
        "input_cost_per_token": 4.35e-07,
        "output_cost_per_token": 8.7e-07,
        "cache_read_input_token_cost": 3.625e-09,
        "cache_creation_input_token_cost": 0.0,
        "input_cost_per_token_cache_hit": 3.625e-09,
    },
}


def register_deepseek_v4_pricing() -> None:
    """Inject DeepSeek V4 pricing into ``litellm.model_cost`` if absent.

    Idempotent and non-destructive: a key already present in the cost map
    (e.g. once litellm ships it) is left untouched. Registers both the bare
    (``deepseek-v4-flash``) and prefixed (``deepseek/deepseek-v4-flash``)
    spellings since ``backfill_cache_pricing`` tries both.
    """
    added = []
    for bare, rates in _DEEPSEEK_V4_RATES.items():
        for key in (bare, f"deepseek/{bare}"):
            if key in litellm.model_cost:
                continue
            entry: dict[str, object] = dict(rates)
            entry["litellm_provider"] = "deepseek"
            entry["mode"] = "chat"
            litellm.model_cost[key] = entry
            added.append(key)
    if added:
        logger.info(
            "Registered temporary DeepSeek V4 pricing shim",
            extra={"models": added},
        )
