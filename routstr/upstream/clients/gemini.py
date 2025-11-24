from __future__ import annotations

from typing import Any, AsyncGenerator, Awaitable, Callable

from openai import AsyncOpenAI

from .base import BaseAPIClient


class GeminiClient(BaseAPIClient):
    """Gemini API client using OpenAI compatibility layer."""

    def __init__(self, api_key: str, base_url: str | None = None):
        super().__init__(api_key, base_url)
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url
            or "https://generativelanguage.googleapis.com/v1beta/openai/",
        )

    async def generate_content(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Generate content using Gemini API via OpenAI SDK (non-streaming)."""
        args = {
            "model": model,
            "messages": messages,
        }
        if temperature is not None:
            args["temperature"] = temperature
        if max_tokens is not None:
            args["max_tokens"] = max_tokens
        if "top_p" in kwargs:
            args["top_p"] = kwargs["top_p"]

        response = await self.client.chat.completions.create(**args)
        return response.model_dump()

    async def generate_content_stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        usage_callback: Callable[[dict[str, Any]], None] | None = None,
        completion_callback: Callable[[str, dict[str, Any] | None], Awaitable[None]]
        | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Generate content using Gemini API via OpenAI SDK (streaming)."""
        args = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if temperature is not None:
            args["temperature"] = temperature
        if max_tokens is not None:
            args["max_tokens"] = max_tokens
        if "top_p" in kwargs:
            args["top_p"] = kwargs["top_p"]

        stream = await self.client.chat.completions.create(**args)

        final_usage = None

        async for chunk in stream:
            chunk_data = chunk.model_dump()

            if chunk.usage:
                final_usage = chunk.usage.model_dump()
                if usage_callback:
                    usage_callback(final_usage)

            yield chunk_data

        if completion_callback:
            await completion_callback(model, final_usage)

    async def list_models(self) -> list[dict[str, Any]]:
        """List available Gemini models."""
        try:
            response = await self.client.models.list()
            return [model.model_dump() for model in response.data]
        except Exception as e:
            from ...core.logging import get_logger

            logger = get_logger(__name__)
            logger.error(f"Failed to list Gemini models: {e}")
            return []
