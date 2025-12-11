from __future__ import annotations

from typing import Any, AsyncGenerator

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
        from openai import NOT_GIVEN

        response = await self.client.chat.completions.create(
            model=model,
            messages=messages,  # type: ignore
            temperature=temperature if temperature is not None else NOT_GIVEN,
            max_tokens=max_tokens if max_tokens is not None else NOT_GIVEN,
            top_p=kwargs.get("top_p", NOT_GIVEN),
        )
        return response.model_dump()

    async def generate_content_stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        from openai import NOT_GIVEN

        usage_callback = kwargs.get("usage_callback")
        completion_callback = kwargs.get("completion_callback")

        stream = await self.client.chat.completions.create(
            model=model,
            messages=messages,  # type: ignore
            stream=True,
            stream_options={"include_usage": True},
            temperature=temperature if temperature is not None else NOT_GIVEN,
            max_tokens=max_tokens if max_tokens is not None else NOT_GIVEN,
            top_p=kwargs.get("top_p", NOT_GIVEN),
        )

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
