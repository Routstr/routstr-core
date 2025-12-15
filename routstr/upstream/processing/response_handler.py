"""Response processing utilities for different API types."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from ...core import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


class ResponseProcessor:
    """Processes responses from upstream services."""

    @staticmethod
    def is_streaming_response(content_str: str) -> bool:
        """Determine if response content indicates streaming.

        Args:
            content_str: Response content as string

        Returns:
            True if response is streaming, False otherwise
        """
        return content_str.startswith("data:") or "data:" in content_str

    @staticmethod
    def extract_usage_from_streaming(content_str: str) -> tuple[dict | None, str | None]:
        """Extract usage data and model from streaming response content.

        Args:
            content_str: Streaming response content as string

        Returns:
            Tuple of (usage_data, model) or (None, None) if not found
        """
        usage_data = None
        model = None

        lines = content_str.strip().split("\n")
        for line in lines:
            if line.startswith("data: "):
                try:
                    data_json = json.loads(line[6:])
                    if "usage" in data_json:
                        usage_data = data_json["usage"]
                        model = data_json.get("model")
                    elif "model" in data_json and not model:
                        model = data_json["model"]
                except json.JSONDecodeError:
                    continue

        return usage_data, model

    @staticmethod
    def clean_response_headers(headers: dict) -> dict:
        """Clean response headers by removing encoding-related headers.

        Args:
            headers: Original response headers

        Returns:
            Cleaned headers dict
        """
        cleaned_headers = dict(headers)
        headers_to_remove = ["transfer-encoding", "content-encoding", "content-length"]

        for header in headers_to_remove:
            cleaned_headers.pop(header, None)

        return cleaned_headers

    @staticmethod
    async def create_streaming_generator(lines: list[str]) -> AsyncGenerator[bytes, None]:
        """Create async generator for streaming response lines.

        Args:
            lines: List of response lines to stream

        Yields:
            Encoded response lines
        """
        for line in lines:
            yield (line + "\n").encode("utf-8")


class ChatCompletionProcessor(ResponseProcessor):
    """Processor specifically for chat completion responses."""

    @staticmethod
    def extract_model_from_chunks(stored_chunks: list[bytes]) -> str | None:
        """Extract model name from stored response chunks.

        Args:
            stored_chunks: List of response chunks from streaming

        Returns:
            Model name if found, None otherwise
        """
        last_model_seen = None

        for i in range(len(stored_chunks) - 1, -1, -1):
            chunk = stored_chunks[i]
            if not chunk:
                continue
            try:
                events = re.split(b"data: ", chunk)
                for event_data in events:
                    if not event_data or event_data.strip() in (b"[DONE]", b""):
                        continue
                    try:
                        data = json.loads(event_data)
                        if isinstance(data, dict) and data.get("model"):
                            return str(data.get("model"))
                    except json.JSONDecodeError:
                        continue
            except Exception as e:
                logger.debug(
                    "Error processing chunk for model extraction",
                    extra={"error": str(e), "error_type": type(e).__name__},
                )

        return last_model_seen


class ResponsesApiProcessor(ResponseProcessor):
    """Processor specifically for Responses API responses."""

    @staticmethod
    def extract_usage_with_reasoning_tokens(content_str: str) -> tuple[dict | None, str | None]:
        """Extract usage data including reasoning tokens from Responses API content.

        Args:
            content_str: Streaming response content as string

        Returns:
            Tuple of (usage_data, model) with reasoning tokens preserved
        """
        usage_data = None
        model = None

        lines = content_str.strip().split("\n")
        for line in lines:
            if line.startswith("data: "):
                try:
                    data_json = json.loads(line[6:])
                    if "usage" in data_json:
                        usage_data = data_json["usage"]
                        model = data_json.get("model")
                        # Note: We now only track input_tokens and output_tokens
                        # reasoning_tokens are ignored per the requirement
                        break
                    elif "model" in data_json and not model:
                        model = data_json["model"]
                except json.JSONDecodeError:
                    continue

        return usage_data, model