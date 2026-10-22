"""Convert a Responses API ``input`` into chat ``messages``.

A port of litellm 1.101.2's ``transform_responses_api_input_to_messages``
(inspection mode: reasoning stays readable ``content``), except that
``input_image`` parts are flattened to ``{image_url: str, detail}``: litellm
dropped ``file_id`` (``url: ""``) and nested a dict-form ``image_url`` as-is.
``file_id`` becomes a sentinel URL the image walker treats as unfetchable.
"""

import json
from collections.abc import Mapping
from typing import Any

from ..core import get_logger

logger = get_logger(__name__)

FILE_ID_URL_PREFIX = "file-id:"


def _flatten_input_image(part: dict[str, Any]) -> tuple[str, str]:
    raw = part.get("image_url")
    url = raw.get("url", "") if isinstance(raw, dict) else raw
    detail = part.get("detail") or (
        raw.get("detail") if isinstance(raw, dict) else None
    )
    if not url and part.get("file_id"):
        url = f"{FILE_ID_URL_PREFIX}{part['file_id']}"
    return (url if isinstance(url, str) else ""), (detail or "auto")


def _normalize_item(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    if item.get("type") == "input_image":
        url, detail = _flatten_input_image(item)
        return {**item, "image_url": url, "detail": detail}
    content = item.get("content")
    if isinstance(content, list):
        return {**item, "content": [_normalize_item(part) for part in content]}
    return item


def input_image_part_to_image_url(part: dict[str, Any]) -> dict[str, Any]:
    """Reshape an ``input_image`` part found inside chat ``messages``."""
    url, detail = _flatten_input_image(part)
    return {"type": "image_url", "image_url": {"url": url, "detail": detail}}


def count_input_images(input_data: Any) -> int:
    if isinstance(input_data, dict):
        own = 1 if input_data.get("type") == "input_image" else 0
        return own + count_input_images(input_data.get("content"))
    if isinstance(input_data, list):
        return sum(count_input_images(item) for item in input_data)
    return 0


def responses_input_to_messages(input_data: Any) -> list[dict[str, Any]] | None:
    """Returns ``None`` when the transform fails so the caller can worst-case."""
    if isinstance(input_data, str):
        return [{"role": "user", "content": input_data}]
    if not isinstance(input_data, list):
        return []
    try:
        return _to_messages([_normalize_item(item) for item in input_data])
    except Exception as e:
        logger.warning(
            "Responses input transform failed; using conservative image fallback",
            extra={"error": str(e)},
        )
        return None


_TOOL_OUTPUT_TYPES = (
    "function_call_output",
    "custom_tool_call_output",
    "web_search_call",
    "computer_call_output",
    "tool_result",
)
_CHAT_CONTENT_TYPES = frozenset(
    {
        "text",
        "image_url",
        "input_audio",
        "audio_url",
        "document",
        "guarded_text",
        "grounding_source",
        "query",
        "video_url",
        "file",
        "thinking",
        "redacted_thinking",
    }
)


def _to_messages(items: list[Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise TypeError(f"Invalid input item: {type(item).__name__}")
        converted = _item_to_messages(item)
        if item.get("type") in ("function_call", "custom_tool_call"):
            if messages and messages[-1].get("role") == "assistant":
                # Consecutive calls share one assistant message.
                messages[-1].setdefault("tool_calls", []).extend(
                    converted[0]["tool_calls"]
                )
                continue
        elif item.get("type") in _TOOL_OUTPUT_TYPES:
            messages.extend(converted)
            continue
        elif _folds_into_tool_calls(messages, converted):
            messages[-1] = {**messages[-1], "content": converted[0]["content"]}
            continue
        messages.extend(converted)
    return messages


def _folds_into_tool_calls(
    messages: list[dict[str, Any]], converted: list[dict[str, Any]]
) -> bool:
    """An assistant reply right after a bare tool_calls message joins it."""
    if not messages or len(converted) != 1:
        return False
    last, new = messages[-1], converted[0]
    return (
        last.get("role") == "assistant"
        and new.get("role") == "assistant"
        and bool(last.get("tool_calls"))
        and not last.get("content")
        and not new.get("tool_calls")
        and new.get("content") is not None
    )


def _item_to_messages(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    item_type = item.get("type")
    if item_type in _TOOL_OUTPUT_TYPES:
        call_id = item.get("call_id")
        if not call_id:
            return []
        return [
            {
                "role": "tool",
                "content": _tool_output_content(item.get("output")),
                "tool_call_id": str(call_id),
            }
        ]
    if item_type in ("function_call", "custom_tool_call"):
        return [
            {"role": "assistant", "content": None, "tool_calls": [_tool_call(item)]}
        ]
    if item_type == "reasoning":
        content = (
            item.get("content")
            if _reasoning_text(item.get("content"), skip_opaque=True) is not None
            else _reasoning_text(item.get("summary")) or item.get("content")
        )
    else:
        content = item.get("content")
    if content is None:
        return []
    return [{"role": item.get("role") or "user", "content": _chat_content(content)}]


def _reasoning_text(blocks: object, skip_opaque: bool = False) -> str | None:
    if skip_opaque and isinstance(blocks, str) and blocks.strip():
        return blocks
    if not isinstance(blocks, list):
        return None
    parts = [
        text.strip()
        for block in blocks
        if isinstance(block, Mapping)
        and not (
            skip_opaque
            and block.get("type") in ("encrypted_content", "redacted_thinking")
        )
        and isinstance(text := block.get("text"), str)
        and text.strip()
    ]
    return "\n".join(parts) if parts else None


def _tool_call(item: Mapping[str, Any]) -> dict[str, Any]:
    arguments = item.get("arguments")
    if not arguments and item.get("type") == "custom_tool_call":
        raw_input = item.get("input") or ""
        arguments = json.dumps({"content": raw_input}) if raw_input else ""
    name = item.get("name") or ""
    namespace = item.get("namespace") or ""
    if namespace and item.get("type") != "custom_tool_call":
        name = f"{namespace}__{name}"
    if not isinstance(arguments, str):
        arguments = "" if arguments is None else json.dumps(arguments, default=str)
    return {
        "id": item.get("call_id") or item.get("id") or "",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
        "index": 0,
    }


def _tool_output_content(output: object) -> Any:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if not isinstance(output, list):
        return json.dumps(output)
    blocks: list[dict[str, Any]] = []
    texts: list[str] = []
    for part in output:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type in ("input_text", "output_text", "text"):
            text = part.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
                blocks.append({"type": "text", "text": text})
        elif part_type in ("input_image", "image_url"):
            value = part.get("image_url") or part.get("url")
            url = value.get("url") if isinstance(value, dict) else value
            if isinstance(url, str) and url:
                blocks.append({"type": "image_url", "image_url": {"url": url}})
    if any(block["type"] == "image_url" for block in blocks):
        return blocks
    return "".join(texts) if texts else json.dumps(output)


def _chat_content(content: object) -> str | list[Any]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ValueError(f"Invalid content type: {type(content)}")
    converted: list[Any] = []
    for part in content:
        if isinstance(part, str):
            converted.append(part)
        elif isinstance(part, dict):
            block = _chat_content_block(part)
            if block is not None:
                converted.append(block)
    return converted


def _chat_content_block(part: dict[str, Any]) -> dict[str, Any] | None:
    part_type = part.get("type")
    if part_type == "input_file":
        file: dict[str, Any] = {}
        if file_id := part.get("file_id") or part.get("file_url"):
            file["file_id"] = file_id
        if part.get("file_data"):
            file["file_data"] = part["file_data"]
        if part.get("filename"):
            file["filename"] = part["filename"]
        block: dict[str, Any] = {"type": "file", "file": file}
    elif part_type == "input_image":
        block = {
            "type": "image_url",
            "image_url": {
                "url": part.get("image_url") or "",
                "detail": part.get("detail") or "auto",
            },
        }
    elif part_type == "encrypted_content":
        encrypted = part.get("encrypted_content")
        return None if encrypted is None else {"type": "text", "text": str(encrypted)}
    elif part.get("text") is None:
        return None
    else:
        block = {"type": _chat_content_type(part_type or "text"), "text": part["text"]}
    if "cache_control" in part:
        block["cache_control"] = part["cache_control"]
    return block


def _chat_content_type(content_type: str) -> str:
    if content_type.startswith("input_"):
        stripped = content_type.removeprefix("input_")
        if stripped in _CHAT_CONTENT_TYPES:
            return stripped
        return "input_audio" if stripped == "audio" else "text"
    if content_type in _CHAT_CONTENT_TYPES:
        return content_type
    return "text"
