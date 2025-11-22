from __future__ import annotations

import time
from typing import Any, AsyncGenerator

import google.generativeai as genai

from .base import BaseAPIClient


class GeminiClient(BaseAPIClient):
    """Native Gemini API client using Google's official package."""

    def __init__(self, api_key: str, base_url: str | None = None):
        super().__init__(api_key, base_url)
        genai.configure(api_key=api_key)  # type: ignore

    def _validate_model_name(self, model: str) -> str:
        return model

    def _convert_openai_to_gemini_messages(self, messages: list[dict[str, Any]]) -> str:
        """Convert OpenAI messages to a simple string for Gemini."""
        combined_content = []

        for message in messages:
            role = message.get("role", "user")
            content = message.get("content", "")

            if role == "system":
                combined_content.append(f"System: {content}")
            elif role == "user":
                combined_content.append(f"User: {content}")
            elif role == "assistant":
                combined_content.append(f"Assistant: {content}")

        return "\n".join(combined_content)

    async def generate_content(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Generate content using Gemini API (non-streaming)."""
        model = self._validate_model_name(model)
        prompt = self._convert_openai_to_gemini_messages(messages)

        generation_config = {}
        if temperature is not None:
            generation_config["temperature"] = temperature
        if max_tokens is not None:
            generation_config["max_output_tokens"] = max_tokens
        if "top_p" in kwargs:
            generation_config["top_p"] = kwargs["top_p"]

        model_instance = genai.GenerativeModel(model)  # type: ignore
        response = await model_instance.generate_content_async(  # type: ignore
            prompt,
            generation_config=generation_config if generation_config else None,  # type: ignore
        )

        return {
            "id": f"chatcmpl-{hash(str(response))}"[1:16],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": response.text,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": getattr(
                    response.usage_metadata, "prompt_token_count", 0
                ),
                "completion_tokens": getattr(
                    response.usage_metadata, "candidates_token_count", 0
                ),
                "total_tokens": getattr(
                    response.usage_metadata, "total_token_count", 0
                ),
            },
        }

    async def generate_content_stream(  # type: ignore[override]
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Generate content using Gemini API (streaming)."""
        model = self._validate_model_name(model)
        prompt = self._convert_openai_to_gemini_messages(messages)

        stream_id = f"chatcmpl-{abs(hash(prompt + str(time.time())))}"[:28]
        created_time = int(time.time())
        generation_config = {}
        if temperature is not None:
            generation_config["temperature"] = temperature
        if max_tokens is not None:
            generation_config["max_output_tokens"] = max_tokens
        if "top_p" in kwargs:
            generation_config["top_p"] = kwargs["top_p"]

        model_instance = genai.GenerativeModel(model)  # type: ignore
        response_stream = await model_instance.generate_content_async(  # type: ignore
            prompt,
            generation_config=generation_config if generation_config else None,  # type: ignore
            stream=True,
        )

        async for chunk in response_stream:
            finish_reason = None
            content = ""

            if hasattr(chunk, "text") and chunk.text:
                content = chunk.text
            if hasattr(chunk, "candidates") and chunk.candidates:
                candidate = chunk.candidates[0]
                finish_reason_raw = getattr(candidate, "finish_reason", None)

                if finish_reason_raw == 1:
                    finish_reason = "stop"
                elif finish_reason_raw == 2:
                    finish_reason = "length"
                elif finish_reason_raw == 3:
                    finish_reason = "content_filter"
                elif finish_reason_raw == 4:
                    finish_reason = "content_filter"
            usage_data = None
            if hasattr(chunk, "usage_metadata") and chunk.usage_metadata:
                usage_metadata = chunk.usage_metadata
                usage_data = {
                    "prompt_tokens": getattr(usage_metadata, "prompt_token_count", 0),
                    "completion_tokens": getattr(
                        usage_metadata, "candidates_token_count", 0
                    ),
                    "total_tokens": getattr(usage_metadata, "total_token_count", 0),
                }

                if hasattr(usage_metadata, "cached_content_token_count"):
                    cached_tokens = getattr(
                        usage_metadata, "cached_content_token_count", 0
                    )
                    if cached_tokens > 0:
                        usage_data["prompt_tokens_details"] = {
                            "cached_tokens": cached_tokens
                        }
            chunk_data = {
                "id": stream_id,
                "object": "chat.completion.chunk",
                "created": created_time,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": content} if content else {},
                        "finish_reason": finish_reason,
                    }
                ],
            }

            if usage_data:
                chunk_data["usage"] = usage_data

            yield chunk_data

            if finish_reason == "stop":
                break

    async def list_models(self) -> list[dict[str, Any]]:
        """List available Gemini models."""
        try:
            models = genai.list_models()  # type: ignore
            return [
                {
                    "name": model.name,
                    "display_name": getattr(model, "display_name", model.name),
                    "description": getattr(model, "description", ""),
                    "supported_generation_methods": getattr(
                        model, "supported_generation_methods", ["generateContent"]
                    ),
                    "input_token_limit": getattr(model, "input_token_limit", 32768),
                    "output_token_limit": getattr(model, "output_token_limit", 8192),
                }
                for model in models
            ]
        except Exception as e:
            from ...core.logging import get_logger

            logger = get_logger(__name__)
            logger.error(f"Failed to list Gemini models: {e}")
            return []
