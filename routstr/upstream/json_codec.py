"""Fast JSON for per-chunk streaming paths, with stdlib fallback.

orjson rejects a few inputs the stdlib accepts (``NaN``/``Infinity`` on load,
non-string keys and integers beyond 64 bits on dump). Streaming must never break
on those, so each call falls back to :mod:`json` instead of raising.
"""

import json

import orjson


def loads(data: bytes | str) -> object | None:
    """Parse JSON, returning ``None`` when the payload is not valid JSON."""
    try:
        return orjson.loads(data)
    except orjson.JSONDecodeError:
        try:
            return json.loads(data)
        except ValueError:
            return None


def dumps(obj: object) -> bytes:
    """Serialize to compact UTF-8 JSON bytes."""
    try:
        return orjson.dumps(obj)
    except TypeError:
        return json.dumps(obj).encode()
