from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI


class GeminiClient:
    """Gemini API client using OpenAI compatibility layer."""

    def __init__(self, api_key: str, base_url: str | None = None):
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url
            or "https://generativelanguage.googleapis.com/v1beta/openai/",
        )

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
