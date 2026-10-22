"""Port of litellm 1.101.2's ``token_counter`` and image tile count.

Tokenizer choice follows litellm's tiktoken path: a name litellm lists as an
OpenAI chat model uses its own encoding (``o200k_base`` whenever it contains
``gpt-4o``); every other name counts as ``gpt-3.5-turbo`` (``cl100k_base``).
litellm's Hugging Face tokenizers are not ported: bare Claude 4 names got a
pre-Claude-3 tokenizer, and the llama ones were downloaded from the Hub
mid-request.

Errors raise, as litellm's did, so callers fall back to their own estimate.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import lzma
import struct
from collections.abc import Callable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

import tiktoken
import tiktoken.model

from ..payment.model_cost import openai_chat_models

DEFAULT_IMAGE_TOKEN_COUNT = 250
_DEFAULT_IMAGE_SIZE = (300, 300)
_IMAGE_BASE_TOKENS = 85
_HIGH_RES_SHORT_SIDE = 768
_HIGH_RES_LONG_SIDE = 2000
_TILE_SIZE = 512
# tiktoken's merge loop is quadratic per regex piece; litellm counts in chunks.
_CHUNK_CHARS = 1024
# litellm keys its Azure deployment names here; only the "-35" spelling changes.
_AZURE_NAMES = frozenset(
    {
        "gpt-35-turbo",
        "gpt-35-turbo-16k",
        "gpt-35-turbo-instruct",
        "azure/gpt-41",
        "azure/gpt-41-mini",
        "azure/gpt-41-nano",
    }
)
# Anthropic block fields counted, in TypedDict order; ids and flags are skipped.
_ANTHROPIC_COUNTED_FIELDS = {
    "tool_use": (("name", "input", "caller"), ("type",)),
    "tool_result": (("content",), ("tool_use_id", "type")),
}

_DATA_DIR = Path(__file__).parent / "data"
# OpenAI's published files (tiktoken_ext.openai_public), xz-compressed.
_VENDORED_ENCODINGS: dict[str, tuple[str, str, dict[str, int]]] = {
    "cl100k_base": (
        "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7",
        r"""'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+| ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s""",
        {
            "<|endoftext|>": 100257,
            "<|fim_prefix|>": 100258,
            "<|fim_middle|>": 100259,
            "<|fim_suffix|>": 100260,
            "<|endofprompt|>": 100276,
        },
    ),
    "o200k_base": (
        "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d",
        "|".join(
            [
                r"""[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?""",
                r"""[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?""",
                r"""\p{N}{1,3}""",
                r""" ?[^\s\p{L}\p{N}]+[\r\n/]*""",
                r"""\s*[\r\n]+""",
                r"""\s+(?!\S)""",
                r"""\s+""",
            ]
        ),
        {"<|endoftext|>": 199999, "<|endofprompt|>": 200018},
    ),
}

Counter = Callable[[str], int]


def _tokenizer_model(model: str) -> str:
    if model in _AZURE_NAMES:
        return model.replace("-35", "-3.5")
    if model in openai_chat_models():
        return model
    return "gpt-3.5-turbo"


@lru_cache(maxsize=256)
def _encoding(model: str) -> tiktoken.Encoding:
    name = _tokenizer_model(model)
    if "gpt-4o" in name:
        return _load_encoding("o200k_base")
    try:
        encoding = tiktoken.model.encoding_name_for_model(name)
    except KeyError:
        return _load_encoding("cl100k_base")
    # Anything else (e.g. o200k_harmony) tiktoken would download, blocking
    # with no timeout; the closest vendored encoding is good enough.
    return _load_encoding(encoding if encoding in _VENDORED_ENCODINGS else "o200k_base")


_LOAD_FAILURES: dict[str, Exception] = {}


def _load_encoding(name: str) -> tiktoken.Encoding:
    """A vendored encoding; a broken file fails fast after the first try."""
    if name in _LOAD_FAILURES:
        raise _LOAD_FAILURES[name]
    try:
        return _read_encoding(name)
    except Exception as exc:
        _LOAD_FAILURES[name] = exc
        raise


@lru_cache(maxsize=None)
def _read_encoding(name: str) -> tiktoken.Encoding:
    sha256, pat_str, special_tokens = _VENDORED_ENCODINGS[name]
    data = lzma.decompress((_DATA_DIR / f"{name}.tiktoken.xz").read_bytes())
    if hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError(f"Vendored {name} does not match its published hash")
    ranks = {
        base64.b64decode(token): int(rank)
        for token, rank in (line.split() for line in data.splitlines() if line)
    }
    return tiktoken.Encoding(
        name=name,
        pat_str=pat_str,
        mergeable_ranks=ranks,
        special_tokens=special_tokens,
    )


def _counter(model: str) -> Counter:
    encoding = _encoding(model)

    def count(text: str) -> int:
        return sum(
            len(encoding.encode(text[i : i + _CHUNK_CHARS], disallowed_special=()))
            for i in range(0, len(text), _CHUNK_CHARS)
        )

    return count


def count_text_tokens(model: str, text: str) -> int:
    return _counter(model)(text)


def count_message_tokens(
    model: str, messages: list[Any], tools: list[Any] | None = None
) -> int:
    """``litellm.token_counter(model, messages=..., tools=...)``."""
    count = _counter(model)
    if _tokenizer_model(model) == "gpt-3.5-turbo-0301":
        per_message, per_name = 4, -1
    else:
        per_message, per_name = 3, 1

    cleaned = []
    for message in messages:
        if not isinstance(message, dict):
            raise TypeError(f"Invalid message type: {type(message)}")
        cleaned.append({k: v for k, v in message.items() if v is not None})

    total = 0
    for message in cleaned:
        total += per_message
        for key, value in message.items():
            if key in ("tool_calls", "function_call"):
                total += _function_call_tokens(key, value, count)
            elif isinstance(value, str):
                total += count(value)
                if key == "name":
                    total += per_name
            elif key == "content" and isinstance(value, list):
                total += _content_list_tokens(value, count)
            elif key == "search_results" and isinstance(value, list):
                text = _search_results_text(value)
                if text:
                    total += count(text)

    total += 3  # every reply is primed with <|start|>assistant<|message|>
    if tools:
        total += count(_format_function_definitions(tools)) + 9
        if any(message.get("role") == "system" for message in cleaned):
            total -= 4
    return total


def _function_call_tokens(key: str, value: object, count: Counter) -> int:
    if key == "tool_calls":
        if not isinstance(value, list):
            raise ValueError(f"Unsupported type {type(value)} for key tool_calls")
        total = 0
        for tool_call in value:
            if "function" not in tool_call:
                raise ValueError("Tool call must contain a function key")
            total += count(str(tool_call["function"].get("arguments", "")))
        return total
    if not isinstance(value, Mapping):
        raise ValueError(f"Unsupported type {type(value)} for key function_call")
    return count(str(value.get("arguments", "")))


def _search_results_text(search_results: list[Any]) -> str:
    texts = ""
    for result in search_results:
        if not isinstance(result, dict):
            continue
        for key in ("source", "title"):
            value = result.get(key)
            if isinstance(value, str):
                texts += value
        content = result.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    texts += block["text"]
        citations = result.get("citations")
        if citations is not None:
            texts += json.dumps(citations, separators=(",", ":"))
    return texts


def _content_list_tokens(content: Any, count: Counter) -> int:
    total = 0
    for block in content:
        if isinstance(block, str):
            total += count(block)
            continue
        block_type = block["type"]
        if block_type == "text":
            total += count(str(block.get("text", "")))
        elif block_type == "image_url":
            total += _image_url_tokens(block.get("image_url"))
        elif block_type == "image":
            _anthropic_source_data(block["source"])
            total += _IMAGE_BASE_TOKENS
        elif block_type == "document":
            total += _document_tokens(block, count)
        elif block_type == "file":
            total += _file_tokens(block.get("file"), count)
        elif block_type in _ANTHROPIC_COUNTED_FIELDS:
            total += _anthropic_block_tokens(block, count)
        elif block_type == "thinking":
            thinking = str(block.get("thinking", ""))
            if thinking:
                total += count(thinking)
        elif block_type == "tool_reference":
            tool_name = str(block.get("tool_name") or "")
            if tool_name:
                total += count(tool_name)
        else:
            raise ValueError(f"Invalid content item type: {block_type}")
    return total


def _image_url_tokens(image_url: object) -> int:
    if isinstance(image_url, dict):
        detail = image_url.get("detail", "auto")
        if detail not in ("low", "high", "auto"):
            raise ValueError(f"Invalid detail value: {detail}")
        url = image_url.get("url")
        if not url:
            raise ValueError("Missing required key 'url' in image_url dict.")
        if detail == "high":
            return image_tokens_high_detail(url)
        return _IMAGE_BASE_TOKENS
    if isinstance(image_url, str):
        if not image_url.strip():
            raise ValueError("Empty image_url string is not valid.")
        return _IMAGE_BASE_TOKENS
    raise ValueError(f"Invalid image_url type: {type(image_url).__name__}")


def _anthropic_source_data(source: Any) -> str:
    if source["type"] == "base64":
        data = source.get("data")
        if not data:
            return ""
        return f"data:{source.get('media_type') or 'image/png'};base64,{data}"
    if source["type"] == "url":
        return source.get("url") or ""
    return ""


def _document_tokens(document: Any, count: Counter) -> int:
    source = document["source"]
    metadata = sum(
        count(text) for text in (document.get("title"), document.get("context")) if text
    )
    if source["type"] == "text":
        return metadata + count(source["data"])
    if source["type"] == "content":
        inner = source["content"]
        if isinstance(inner, str):
            return metadata + count(inner)
        return metadata + _content_list_tokens(inner, count)
    _anthropic_source_data(source)
    return metadata + _IMAGE_BASE_TOKENS


def _file_tokens(file_value: object, count: Counter) -> int:
    if not isinstance(file_value, Mapping):
        return 0
    filename = file_value.get("filename")
    file_data = file_value.get("file_data")
    name_tokens = count(filename) if isinstance(filename, str) and filename else 0
    if not isinstance(file_data, str) or not file_data:
        return name_tokens
    return name_tokens + _IMAGE_BASE_TOKENS


def _anthropic_block_tokens(block: Mapping[str, Any], count: Counter) -> int:
    fields, required = _ANTHROPIC_COUNTED_FIELDS[block["type"]]
    missing = [key for key in required if key not in block]
    if missing:
        raise ValueError(f"Missing required fields in {block['type']} block")
    total = 0
    for field in fields:
        value = block.get(field)
        if isinstance(value, str):
            total += count(value)
        elif isinstance(value, list):
            total += _content_list_tokens(value, count)
        elif isinstance(value, dict):
            total += count(str(value))
    return total


def _format_function_definitions(tools: list[Any]) -> str:
    """OpenAI's apparent TypeScript-like rendering of tool definitions."""
    lines = ["namespace functions {", ""]
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            params = tool.get("input_schema") or tool.get("parameters") or {}
            function = {
                "name": tool.get("name"),
                "description": tool.get("description"),
                "parameters": params if isinstance(params, dict) else {},
            }
        name = function.get("name")
        if not name:
            continue
        if description := function.get("description"):
            lines.append(f"// {description}")
        parameters = function.get("parameters") or {}
        if not isinstance(parameters, dict):
            parameters = {}
        properties = parameters.get("properties")
        if properties and properties.keys():
            lines.append(f"type {name} = (_: {{")
            lines.append(_format_object_parameters(parameters, 0))
            lines.append("}) => any;")
        else:
            lines.append(f"type {name} = () => any;")
        lines.append("")
    lines.append("} // namespace functions")
    return "\n".join(lines)


def _format_object_parameters(parameters: dict[str, Any], indent: int) -> str:
    properties = parameters.get("properties")
    if not properties:
        return ""
    required = parameters.get("required", [])
    lines = []
    for key, props in properties.items():
        if description := props.get("description"):
            lines.append(f"// {description}")
        optional = "" if required and key in required else "?"
        lines.append(f"{key}{optional}: {_format_type(props, indent)},")
    return "\n".join(" " * max(0, indent) + line for line in lines)


def _format_type(props: dict[str, Any], indent: int) -> str:
    kind = props.get("type")
    if kind == "string":
        if "enum" in props:
            return " | ".join(f'"{item}"' for item in props["enum"])
        return "string"
    if kind == "array":
        return f"{_format_type(props['items'], indent)}[]"
    if kind == "object":
        return f"{{\n{_format_object_parameters(props, indent + 2)}\n}}"
    if kind in ("integer", "number"):
        if "enum" in props:
            return " | ".join(f'"{item}"' for item in props["enum"])
        return "number"
    if kind in ("boolean", "null"):
        return kind
    return "any"


def image_tokens_high_detail(url: str) -> int:
    """OpenAI high-detail tiles. litellm fetched remote URLs; they get 300x300."""
    if url.startswith(("http://", "https://")):
        width, height = _DEFAULT_IMAGE_SIZE
    else:
        width, height = _resize_high_res(*_image_dimensions(url))
    tiles = ((width + _TILE_SIZE - 1) // _TILE_SIZE) * (
        (height + _TILE_SIZE - 1) // _TILE_SIZE
    )
    return _IMAGE_BASE_TOKENS + _IMAGE_BASE_TOKENS * 2 * tiles


def _resize_high_res(width: int, height: int) -> tuple[int, int]:
    if width <= _HIGH_RES_SHORT_SIDE and height <= _HIGH_RES_SHORT_SIDE:
        return width, height
    aspect = max(width, height) / min(width, height)
    short, long = _HIGH_RES_SHORT_SIDE, int(_HIGH_RES_SHORT_SIDE * aspect)
    if long > _HIGH_RES_LONG_SIDE:
        long = _HIGH_RES_LONG_SIDE
        short = int(long / aspect)
    return (short, long) if width <= height else (long, short)


def _image_dimensions(data_url: str) -> tuple[int, int]:
    """Header-sniffed size; unknown formats get litellm's 300x300 default."""
    _, encoded = data_url.split(",", 1)
    data = base64.b64decode(encoded)
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        width, height = struct.unpack(">LL", data[16:24])
        return width, height
    if data[:4] == b"GIF8" and data[5:6] == b"a":
        width, height = struct.unpack("<HH", data[6:10])
        return width, height
    if data[:3] == b"\xff\xd8\xff":
        return _jpeg_dimensions(data)
    if data[4:8] == b"ftyp":
        return _DEFAULT_IMAGE_SIZE
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X":
            width = struct.unpack("<I", data[24:27] + b"\x00")[0] + 1
            height = struct.unpack("<I", data[27:30] + b"\x00")[0] + 1
            return width, height
        if chunk == b"VP8 ":
            width = struct.unpack("<H", data[26:28])[0] & 0x3FFF
            height = struct.unpack("<H", data[28:30])[0] & 0x3FFF
            return width, height
        if chunk == b"VP8L":
            bits = struct.unpack("<I", data[21:25])[0]
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return _DEFAULT_IMAGE_SIZE


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    with io.BytesIO(data) as handle:
        size, marker = 2, 0
        while not 0xC0 <= marker <= 0xCF or marker in (0xC4, 0xC8, 0xCC):
            handle.seek(size, 1)
            byte = handle.read(1)
            while ord(byte) == 0xFF:
                byte = handle.read(1)
            marker = ord(byte)
            size = struct.unpack(">H", handle.read(2))[0] - 2
        handle.seek(1, 1)
        height, width = struct.unpack(">HH", handle.read(4))
    return width, height
