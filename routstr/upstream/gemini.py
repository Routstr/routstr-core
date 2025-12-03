from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from .base import BaseUpstreamProvider
from .clients.gemini import GeminiClient

if TYPE_CHECKING:
    from ..core.db import ApiKey, AsyncSession, UpstreamProviderRow
    from ..payment.models import Model

from ..core.logging import get_logger

logger = get_logger(__name__)


class GeminiUpstreamProvider(BaseUpstreamProvider):
    provider_type = "gemini"
    default_base_url = "https://generativelanguage.googleapis.com/v1beta"
    platform_url = "https://aistudio.google.com/app/apikey"

    def __init__(
        self,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        api_key: str = "",
        provider_fee: float = 1.01,
    ):
        super().__init__(
            api_key=api_key,
            provider_fee=provider_fee,
            base_url=base_url,
        )
        self._client: GeminiClient | None = None

    @property
    def client(self) -> GeminiClient:
        """Get or create the Gemini API client."""
        if self._client is None:
            self._client = GeminiClient(api_key=self.api_key)
        return self._client

    @classmethod
    def from_db_row(
        cls, provider_row: "UpstreamProviderRow"
    ) -> "GeminiUpstreamProvider":
        return cls(
            base_url=provider_row.base_url,
            api_key=provider_row.api_key,
            provider_fee=provider_row.provider_fee,
        )

    @classmethod
    def get_provider_metadata(cls) -> dict[str, object]:
        return {
            "id": cls.provider_type,
            "name": "Google Gemini",
            "default_base_url": cls.default_base_url,
            "fixed_base_url": True,
            "platform_url": cls.platform_url,
        }

    def transform_model_name(self, model_id: str) -> str:
        return model_id.removeprefix("gemini/")

    async def forward_request(
        self,
        request: Request,
        path: str,
        headers: dict,
        request_body: bytes | None,
        key: ApiKey,
        max_cost_for_model: int,
        session: AsyncSession,
        model_obj: Model,
    ) -> Response | StreamingResponse:
        # Remove provider prefix from model ID for Gemini API
        if "/" in model_obj.id:
            model_obj.id = model_obj.id.split("/", 1)[1]

        if not path.startswith("chat/completions"):
            return await super().forward_request(
                request,
                path,
                headers,
                request_body,
                key,
                max_cost_for_model,
                session,
                model_obj,
            )

        if not request_body:
            return await super().forward_request(
                request,
                path,
                headers,
                request_body,
                key,
                max_cost_for_model,
                session,
                model_obj,
            )

        try:
            openai_data = json.loads(request_body)
            messages = openai_data.get("messages", [])
            temperature = openai_data.get("temperature")
            max_tokens = openai_data.get("max_tokens")
            top_p = openai_data.get("top_p")
            is_streaming = openai_data.get("stream", False)

            logger.info(
                "Processing Gemini request with client abstraction",
                extra={
                    "model": model_obj.id,
                    "is_streaming": is_streaming,
                    "message_count": len(messages),
                    "key_hash": key.hashed_key[:8] + "...",
                },
            )

            if is_streaming:
                final_usage_data: dict | None = None

                def usage_callback(usage_data: dict[str, Any]) -> None:
                    """Callback to capture usage data during streaming"""
                    nonlocal final_usage_data
                    final_usage_data = usage_data

                async def completion_callback(
                    model: str, usage_data: dict[str, Any] | None
                ) -> None:
                    """Callback to handle payment when streaming completes"""
                    nonlocal final_usage_data
                    if usage_data:
                        final_usage_data = usage_data

                    payment_data = {
                        "model": model,
                        "usage": final_usage_data,
                    }

                    from ..auth import adjust_payment_for_tokens
                    from ..core.db import create_session

                    async with create_session() as new_session:
                        fresh_key = await new_session.get(key.__class__, key.hashed_key)
                        if fresh_key:
                            try:
                                cost_data = await adjust_payment_for_tokens(
                                    fresh_key,
                                    payment_data,
                                    new_session,
                                    max_cost_for_model,
                                )

                                logger.info(
                                    "Gemini streaming payment finalized",
                                    extra={
                                        "cost_data": cost_data,
                                        "usage_data": final_usage_data,
                                        "key_hash": key.hashed_key[:8] + "...",
                                    },
                                )
                            except Exception as cost_error:
                                logger.error(
                                    "Error finalizing Gemini streaming payment",
                                    extra={
                                        "error": str(cost_error),
                                        "key_hash": key.hashed_key[:8] + "...",
                                    },
                                )

                response_generator = self.client.generate_content_stream(
                    model=model_obj.id,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    top_p=top_p,
                    usage_callback=usage_callback,
                    completion_callback=completion_callback,
                )

                async def stream_with_cost() -> AsyncGenerator[bytes, None]:
                    try:
                        async for chunk in response_generator:
                            sse_data = f"data: {json.dumps(chunk)}\n\n"
                            yield sse_data.encode()

                    except Exception as e:
                        logger.error(
                            "Error in Gemini streaming response",
                            extra={
                                "error": str(e),
                                "error_type": type(e).__name__,
                                "key_hash": key.hashed_key[:8] + "...",
                            },
                        )
                        raise

                return StreamingResponse(
                    stream_with_cost(),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
                )

            else:
                openai_format_response = await self.client.generate_content(
                    model=model_obj.id,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    top_p=top_p,
                )

                from ..auth import adjust_payment_for_tokens

                cost_data = await adjust_payment_for_tokens(
                    key, openai_format_response, session, max_cost_for_model
                )
                openai_format_response["cost"] = cost_data

                logger.info(
                    "Gemini non-streaming payment completed",
                    extra={
                        "cost_data": cost_data,
                        "model": model_obj.id,
                        "key_hash": key.hashed_key[:8] + "...",
                    },
                )

                return Response(
                    content=json.dumps(openai_format_response),
                    media_type="application/json",
                    headers={"Cache-Control": "no-cache"},
                )

        except Exception as e:
            logger.error(
                "Error in Gemini forward_request",
                extra={
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "path": path,
                    "key_hash": key.hashed_key[:8] + "...",
                },
            )
            return await super().forward_request(
                request,
                path,
                headers,
                request_body,
                key,
                max_cost_for_model,
                session,
                model_obj,
            )

    async def _fetch_provider_models(self) -> dict:
        """Fetch models from Gemini API."""
        try:
            models_data = await self.client.list_models()

            for model in models_data:
                if "id" in model and model["id"].startswith("models/"):
                    model["id"] = model["id"].removeprefix("models/")

            return {"data": models_data}
        except Exception as e:
            logger.error(
                f"Failed to fetch models from Gemini API: {e}",
                extra={
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "base_url": self.base_url,
                },
            )
            return {"data": []}
