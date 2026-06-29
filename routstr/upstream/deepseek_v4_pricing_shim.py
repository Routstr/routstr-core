"""TEMPORARY: local DeepSeek V4 pricing shim.

litellm's bundled cost map does not yet ship ``deepseek-v4-flash`` /
``deepseek-v4-pro``. Without an entry, ``backfill_cache_pricing`` cannot find a
``cache_read_input_token_cost`` and cache reads fall back to the full input
rate — a ~60% overcharge on cache hits (DeepSeek hits are ~0.2x input).

This module injects the missing entries into ``litellm.model_cost`` at startup
so the existing backfill path resolves them. Rates mirror the open upstream PR
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

# USD per token. Source: BerriAI/litellm PR #26380.
_DEEPSEEK_V4_RATES: dict[str, dict[str, float]] = {
    "deepseek-v4-flash": {
        "input_cost_per_token": 1.4e-07,
        "output_cost_per_token": 2.8e-07,
        "cache_read_input_token_cost": 2.8e-08,
        "cache_creation_input_token_cost": 0.0,
        "input_cost_per_token_cache_hit": 2.8e-08,
    },
    "deepseek-v4-pro": {
        "input_cost_per_token": 1.74e-06,
        "output_cost_per_token": 3.48e-06,
        "cache_read_input_token_cost": 1.4e-07,
        "cache_creation_input_token_cost": 0.0,
        "input_cost_per_token_cache_hit": 1.4e-07,
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
