"""The one definition of a billable rate, with no dependencies of its own.

A rate reaches the node from an upstream catalog, the LiteLLM cost map, an
operator's admin edit, a legacy database row and the BTC/USD feed. Each of those
readers needs the same two questions answered — is this value a rate at all, and
is it a rate a request can be billed on — so both answers live here, in a module
that imports nothing from the package. Every guard then shares one definition
instead of drifting, and no caller needs a deferred import to reach it.
"""

import math

# The rates a request can bill on. Derived fields (``max_*_cost``) are excluded
# — they are computed carriers, not charged rates. One definition, shared by the
# admin write edge and the served/routed guards, so they all cover the same set.
BILLABLE_PRICING_FIELDS = (
    "prompt",
    "completion",
    "request",
    "image",
    "web_search",
    "internal_reasoning",
    "input_cache_read",
    "input_cache_write",
)


def is_usable_rate(rate: float) -> bool:
    """True if a single billable rate is a number a request could be billed on.

    The one definition of a usable rate, so every guard that asks the question
    answers it identically. A rate qualifies only when it is finite and
    non-negative; zero is usable (it means "free", which is a real price) but
    ``NaN``, ``±inf`` and negatives are not prices at all.

    Non-finite: ``inf > 0`` is True, so an infinite rate reads as chargeable and
    would be served, routed and billed as ``inf``; ``NaN`` poisons every total it
    enters and defeats ordinary comparisons, since ``NaN > 0``, ``NaN < 0`` and
    ``NaN == 0`` are all False. Negative: a negative rate produces a negative
    cost, which the settlement path subtracts from the balance — it pays the
    caller to make requests. Both reach a stored row from upstream catalogs as
    well as the admin edge (``json.loads`` accepts the bare ``NaN``/``Infinity``
    literals and overflows ``1e999`` to ``inf``).

    This is the rationale for every guard that calls it; the call sites say what
    they do with the answer, not why the answer matters.
    """
    return math.isfinite(rate) and rate >= 0.0
