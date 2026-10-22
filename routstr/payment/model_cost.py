"""Per-model USD rates vendored from litellm's cost map.

``data/model_cost.json`` is a pruned snapshot; regenerate it with
``scripts/update_model_cost.py``.
"""

import json
from functools import lru_cache
from pathlib import Path

_DATA = Path(__file__).parent / "data" / "model_cost.json"


@lru_cache(maxsize=1)
def model_cost() -> dict[str, dict]:
    return json.loads(_DATA.read_text())


@lru_cache(maxsize=1)
def openai_chat_models() -> frozenset[str]:
    """Names litellm treats as OpenAI chat models (fine-tunes excluded)."""
    return frozenset(
        key
        for key, info in model_cost().items()
        if info.get("litellm_provider") == "openai"
        and not (key.startswith("ft:") and key.count(":") <= 1)
    )


def model_cost_entry(model_id: str) -> dict | None:
    """Look up ``model_id`` in the vendored cost map.

    Keys are the exact OpenRouter id (``deepseek/deepseek-chat``) or the bare
    model name (``gpt-4o``, ``claude-sonnet-4-5``), so both spellings are tried.
    Keys are lowercase, so a mixed-case upstream id
    (``deepseek-ai/DeepSeek-V4-Flash``) is retried via a case-insensitive scan.
    Returns the matched cost dict, or ``None``.
    """
    costs = model_cost()
    candidates = (model_id, model_id.split("/", 1)[-1])
    for key in candidates:
        info = costs.get(key)
        if isinstance(info, dict):
            return info

    lowered = {c.lower() for c in candidates}
    for key, info in costs.items():
        if key.lower() in lowered and isinstance(info, dict):
            return info
    return None
