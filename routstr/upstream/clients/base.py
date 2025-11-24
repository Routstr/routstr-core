from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator


class BaseAPIClient(ABC):
    """Base class for AI provider API clients."""

    def __init__(self, api_key: str, base_url: str | None = None):
        self.api_key = api_key
        self.base_url = base_url

    @abstractmethod
    async def generate_content(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Generate content non-streaming."""
        pass

    @abstractmethod
    def generate_content_stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        pass

    @abstractmethod
    async def list_models(self) -> list[dict[str, Any]]:
        """List available models."""
        pass
