"""Mistral, Moonshot and OpenRouter chat rules (litellm 1.101.2 configs)."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from . import model_capabilities as caps
from .anthropic_openai_stream import StreamPayloadError
from .chat_params import (
    ChatProvider,
    Messages,
    Params,
    gpt_params,
    max_tokens_rename,
    supports_reasoning,
)


def content_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text") or "" for c in content if isinstance(c, dict))
    return ""


def _flatten_text_content(messages: Messages) -> Messages:
    """litellm ``handle_messages_with_content_list_to_str_conversion``."""
    flattened: Messages = []
    for m in messages:
        text = content_text(m) if m.get("content") else ""
        flattened.append({**m, "content": text} if text else m)
    return flattened


def _mistral_supported(model: str) -> set[str]:
    supported = {
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "tools",
        "tool_choice",
        "stop",
        "response_format",
        "parallel_tool_calls",
    }
    if "magistral" in model.lower():
        supported.add("reasoning_effort")
    return supported


def _mistral_map(params: Params, model: str) -> Params:
    out: Params = {}
    for key, value in params.items():
        if key in ("max_tokens", "max_completion_tokens"):
            out["max_tokens"] = value
        elif key == "tools":
            out["tools"] = _strip_schema_refs(copy.deepcopy(value))
        elif key in (
            "temperature",
            "top_p",
            "stop",
            "response_format",
            "parallel_tool_calls",
        ):
            out[key] = value
        elif key == "tool_choice" and isinstance(value, str):
            out["tool_choice"] = "any" if value == "required" else value
        elif key == "reasoning_effort" and "magistral" in model.lower():
            out["_add_reasoning_prompt"] = True
    return out


def _strip_schema_refs(schema: Any, depth: int = 10) -> Any:
    if depth <= 0:
        return schema
    if isinstance(schema, dict):
        schema.pop("$id", None)
        schema.pop("$schema", None)
        for value in schema.values():
            _strip_schema_refs(value, depth - 1)
    elif isinstance(schema, list):
        for item in schema:
            _strip_schema_refs(item, depth - 1)
    return schema


MISTRAL_REASONING_PROMPT = """
        <s>[SYSTEM_PROMPT]system_prompt
        A user will ask you to solve a task. You should first draft your thinking process (inner monologue) until you have derived the final answer. Afterwards, write a self-contained summary of your thoughts (i.e. your summary should be succinct but contain all the critical steps you needed to reach the conclusion). You should use Markdown to format your response. Write both your thoughts and summary in the same language as the task posed by the user. NEVER use \\boxed{} in your response.

        Your thinking process must follow the template below:
        <think>
        Your thoughts or/and draft, like working through an exercise on scratch paper. Be as casual and as long as you want until you are confident to generate a correct answer.
        </think>

        Here, provide a concise summary that reflects your reasoning and presents a clear final answer to the user. Don't mention that this is a summary.

        Problem:

        [/SYSTEM_PROMPT][INST]user_message[/INST]<think>
        reasoning_traces
        </think>
        assistant_response</s>[INST]user_message[/INST]
        """


def _mistral_transform(messages: Messages, params: Params, model: str) -> Messages:
    messages = [
        {
            k: v
            for k, v in m.items()
            if k not in ("reasoning_content", "thinking_blocks")
        }
        if m.get("role") == "assistant"
        else m
        for m in messages
    ]
    has_media = any(
        isinstance(m.get("content"), list)
        and any(c.get("type") in ("image_url", "file") for c in m["content"])
        for m in messages
    )
    if not has_media:
        cleaned: Messages = []
        for m in _flatten_text_content(messages):
            if m.get("name") is not None and (
                m["role"] != "tool" or not str(m["name"]).strip()
            ):
                m = {k: v for k, v in m.items() if k != "name"}
            if isinstance(m.get("tool_calls"), list):
                m = {
                    **m,
                    "tool_calls": [
                        {
                            "id": t.get("id"),
                            "type": "function",
                            "function": t.get("function"),
                        }
                        for t in m["tool_calls"]
                    ],
                }
            if m.get("role") == "assistant" and all(
                v is None or (k == "content" and v == "")
                for k, v in m.items()
                if k != "role"
            ):
                continue
            cleaned.append({k: v for k, v in m.items() if v is not None})
        messages = cleaned
    if params.pop("_add_reasoning_prompt", False) and "magistral" in model.lower():
        messages = _with_reasoning_prompt(messages)
    return messages


def _with_reasoning_prompt(messages: Messages) -> Messages:
    for i, m in enumerate(messages):
        if m.get("role") != "system":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            new_content: Any = [
                {"type": "text", "text": MISTRAL_REASONING_PROMPT + "\n\n"},
                *content,
            ]
        else:
            new_content = f"{MISTRAL_REASONING_PROMPT}\n\n{content}"
        return [*messages[:i], {**m, "content": new_content}, *messages[i + 1 :]]
    return [{"role": "system", "content": MISTRAL_REASONING_PROMPT}, *messages]


def _moonshot_supported(model: str) -> set[str]:
    excluded = {"tools", "tool_choice"} if "kimi-thinking-preview" in model else set()
    supported = gpt_params(model) - excluded
    if supports_reasoning(model, "moonshot"):
        supported.add("reasoning_effort")
    return supported


def _moonshot_map(params: Params, model: str) -> Params:
    out = max_tokens_rename(params, model)
    if supports_reasoning(model, "moonshot"):
        out.pop("temperature", None)
    elif "temperature" in out:
        out["temperature"] = min(out["temperature"], 1)
    return out


def _moonshot_transform(messages: Messages, params: Params, model: str) -> Messages:
    if params.get("tool_choice") == "required":
        params.pop("tool_choice")
        messages = [
            *messages,
            {
                "role": "user",
                "content": "Please select a tool to handle the current issue.",
            },
        ]
    if supports_reasoning(model, "moonshot"):
        messages = [
            {**m, "reasoning_content": " "}
            if m.get("role") == "assistant"
            and m.get("tool_calls")
            and not m.get("reasoning_content")
            else m
            for m in messages
        ]
    if not any(
        isinstance(m.get("content"), list)
        and any(c.get("type") != "text" for c in m["content"])
        for m in messages
    ):
        messages = _flatten_text_content(messages)
    return messages


_OPENROUTER_CACHE_MODELS = ("claude", "gemini", "minimax", "glm", "z-ai")


def _openrouter_supported(model: str) -> set[str]:
    supported = gpt_params(model)
    if supports_reasoning(model, "openrouter") or caps.supports(
        model, None, "supports_reasoning"
    ):
        supported.add("reasoning_effort")
    return supported


def _openrouter_map(params: Params, _model: str) -> Params:
    if params.get("reasoning_effort") == "max":
        return {**params, "reasoning_effort": "xhigh"}
    return params


def _openrouter_transform(messages: Messages, params: Params, model: str) -> Messages:
    params.setdefault("usage", {"include": True})
    if not any(name in model.lower() for name in _OPENROUTER_CACHE_MODELS):
        return messages
    moved: Messages = []
    for m in messages:
        m = dict(m)
        cache_control = m.pop("cache_control", None)
        if cache_control is not None:
            content = m.get("content")
            if isinstance(content, list):
                if content:
                    m["content"] = [
                        {**block, "cache_control": cache_control}
                        if i == len(content) - 1
                        else dict(block)
                        for i, block in enumerate(content)
                    ]
            else:
                m["content"] = [
                    {"type": "text", "text": content, "cache_control": cache_control}
                ]
        moved.append(m)
    return moved


def _openrouter_chunk(chunk: dict) -> dict:
    """litellm's OpenRouter stream parser: ``reasoning`` wins, errors reworded."""
    error = chunk.get("error")
    if error is not None:
        code = error.get("code") if isinstance(error, dict) else None
        raise StreamPayloadError(
            f"Message: {error.get('message') if isinstance(error, dict) else error}, "
            f"Metadata: {error.get('metadata', {}) if isinstance(error, dict) else {}}, "
            f"User ID: {error.get('user_id', '') if isinstance(error, dict) else ''}",
            code if isinstance(code, int) else 500,
        )
    for choice in chunk.get("choices") or ():
        delta = choice.get("delta")
        if isinstance(delta, dict):
            delta["reasoning_content"] = delta.get("reasoning")
    return chunk


EXTRA_PROVIDERS: dict[str, Callable[[str], ChatProvider]] = {
    "mistral": lambda _m: ChatProvider(
        supported=_mistral_supported,
        map_params=_mistral_map,
        transform=_mistral_transform,
    ),
    "moonshot": lambda _m: ChatProvider(
        supported=_moonshot_supported,
        map_params=_moonshot_map,
        transform=_moonshot_transform,
        sdk=True,
    ),
    "openrouter": lambda m: ChatProvider(
        supported=_openrouter_supported,
        map_params=_openrouter_map,
        transform=_openrouter_transform,
        strip_cache_control=not any(n in m.lower() for n in _OPENROUTER_CACHE_MODELS),
    ),
}

CHUNK_HOOKS: dict[str, Callable[[dict], dict]] = {"openrouter": _openrouter_chunk}
