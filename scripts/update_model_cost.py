#!/usr/bin/env python3
"""Regenerate ``routstr/payment/data/model_cost.json`` from litellm's cost map.

Pruning rule:

1. Drop the reserved top-level keys (``sample_spec``,
   ``fallback_generalizations``) and any non-object entry.
2. Expand each entry's ``aliases`` into top-level keys sharing its data, as
   litellm does at load time: a canonical key or an earlier alias wins.
3. Keep only the fields routstr reads (``KEPT_FIELDS``), dropping nulls but
   not explicit ``false`` flags, which the /v1/messages param filter reads.
   ``mode`` is kept only when it is ``responses``, the one value checked.

Every entry is kept, even with no field left: dropping a key would let the
bare-name fallback resolve some other entry's prices. Source order is kept
too, since the case-insensitive lookup returns the first match.

Usage: python scripts/update_model_cost.py [source-url-or-path]
"""

import json
import sys
from pathlib import Path

import httpx

SOURCE_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
OUTPUT = Path(__file__).resolve().parent.parent / "routstr/payment/data/model_cost.json"
RESERVED_KEYS = {"sample_spec", "fallback_generalizations"}
COST_FIELDS = (
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_read_input_token_cost",
    "cache_creation_input_token_cost",
)
CAPABILITY_FIELDS = (
    "supports_vision",
    "supports_reasoning",
    "supports_function_calling",
    "supports_tool_choice",
    "supports_web_search",
    "supports_none_reasoning_effort",
    "supports_minimal_reasoning_effort",
    "supports_xhigh_reasoning_effort",
    "reasoning_effort_levels",
    "default_reasoning_effort",
    "supports_low_reasoning_effort",
    "supports_max_reasoning_effort",
    "supports_response_schema",
    "supports_parallel_function_calling",
    "supports_system_messages",
)
KEPT_FIELDS = (
    "litellm_provider",
    "mode",
    *COST_FIELDS,
    "max_input_tokens",
    "max_output_tokens",
    *CAPABILITY_FIELDS,
)


def load_source(source: str) -> dict:
    if source.startswith(("http://", "https://")):
        response = httpx.get(source, timeout=30)
        response.raise_for_status()
        return response.json()
    return json.loads(Path(source).read_text())


def expand_aliases(raw: dict) -> dict[str, dict]:
    entries = {
        key: value
        for key, value in raw.items()
        if key not in RESERVED_KEYS and isinstance(value, dict)
    }
    aliases: dict[str, dict] = {}
    for info in entries.values():
        names = info.get("aliases")
        if not isinstance(names, list):
            continue
        for alias in names:
            if alias not in entries and alias not in aliases:
                aliases[alias] = info
    return {**entries, **aliases}


def prune(entries: dict[str, dict]) -> dict[str, dict]:
    return {key: _kept_fields(info) for key, info in entries.items()}


def _kept_fields(info: dict) -> dict:
    kept = {f: info[f] for f in KEPT_FIELDS if info.get(f) is not None}
    if kept.get("mode") != "responses":
        kept.pop("mode", None)
    return kept


def dump(entries: dict[str, dict]) -> str:
    lines = [
        f"{json.dumps(k)}:{json.dumps(v, separators=(',', ':'))}"
        for k, v in entries.items()
    ]
    return "{\n" + ",\n".join(lines) + "\n}\n"


def main() -> None:
    source = sys.argv[1] if len(sys.argv) > 1 else SOURCE_URL
    pruned = prune(expand_aliases(load_source(source)))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(dump(pruned))
    print(f"wrote {len(pruned)} entries to {OUTPUT}")


if __name__ == "__main__":
    main()
