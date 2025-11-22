from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

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

    def prepare_headers(self, request_headers: dict) -> dict:
        headers = dict(request_headers)
        removed_headers = []

        for header in [
            "host",
            "content-length",
            "refund-lnurl",
            "key-expiry-time",
            "x-cashu",
            "authorization",
        ]:
            if headers.pop(header, None) is not None:
                removed_headers.append(header)

        if self.api_key:
            headers["x-goog-api-key"] = self.api_key

        return headers

    def transform_model_name(self, model_id: str) -> str:
        return model_id.removeprefix("gemini/")

    def prepare_request_body(
        self, body: bytes | None, model_obj: Model
    ) -> bytes | None:
        if not body:
            return body

        try:
            openai_data = json.loads(body)
            if not isinstance(openai_data, dict):
                return body

            gemini_data = {}

            if "messages" in openai_data:
                contents = []
                for message in openai_data["messages"]:
                    role = message.get("role", "user")
                    content = message.get("content", "")

                    if role == "system":
                        continue
                    elif role == "assistant":
                        role = "model"

                    contents.append({"role": role, "parts": [{"text": content}]})
                gemini_data["contents"] = contents

            generation_config = {}
            if "temperature" in openai_data:
                generation_config["temperature"] = openai_data["temperature"]
            if "max_tokens" in openai_data:
                generation_config["maxOutputTokens"] = openai_data["max_tokens"]
            if "top_p" in openai_data:
                generation_config["topP"] = openai_data["top_p"]

            if generation_config:
                gemini_data["generationConfig"] = generation_config  # type: ignore

            return json.dumps(gemini_data).encode()

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(
                f"Failed to transform request body for Gemini: {e}",
                extra={"error": str(e), "error_type": type(e).__name__},
            )
            return body

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
                response_generator = self.client.generate_content_stream(
                    model=model_obj.id,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    top_p=top_p,
                )

                async def stream_with_cost() -> AsyncGenerator[bytes, None]:
                    last_model_seen: str = model_obj.id
                    final_usage_data: dict | None = None

                    try:
                        async for chunk in response_generator:
                            if chunk.get("usage"):
                                final_usage_data = chunk["usage"]

                            sse_data = f"data: {json.dumps(chunk)}\n\n"
                            yield sse_data.encode()

                            if (
                                chunk.get("choices", [{}])[0].get("finish_reason")
                                == "stop"
                            ):
                                break

                        payment_data = {
                            "model": last_model_seen,
                            "usage": final_usage_data,
                        }

                        from ..auth import adjust_payment_for_tokens
                        from ..core.db import create_session

                        async with create_session() as new_session:
                            fresh_key = await new_session.get(
                                key.__class__, key.hashed_key
                            )
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

    def _transform_gemini_to_openai(self, gemini_response: dict, model_id: str) -> dict:
        """Transform Gemini API response to OpenAI format."""
        candidates = gemini_response.get("candidates", [])
        if not candidates:
            return {
                "id": f"chatcmpl-{hash(str(gemini_response))}"[1:16],
                "object": "chat.completion",
                "created": int(__import__("time").time()),
                "model": model_id,
                "choices": [],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            }

        first_candidate = candidates[0]
        content = ""
        if "content" in first_candidate and "parts" in first_candidate["content"]:
            parts = first_candidate["content"]["parts"]
            if parts and "text" in parts[0]:
                content = parts[0]["text"]

        finish_reason = first_candidate.get("finishReason", "stop")
        if finish_reason == "STOP":
            finish_reason = "stop"
        elif finish_reason == "MAX_TOKENS":
            finish_reason = "length"

        usage_metadata = gemini_response.get("usageMetadata", {})
        prompt_tokens = usage_metadata.get("promptTokenCount", 0)
        completion_tokens = usage_metadata.get("candidatesTokenCount", 0)

        return {
            "id": f"chatcmpl-{hash(str(gemini_response))}"[1:16],
            "object": "chat.completion",
            "created": int(__import__("time").time()),
            "model": model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content,
                    },
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    async def fetch_models(self) -> list[Model]:
        from ..payment.models import Architecture, Model, Pricing, TopProvider

        try:
            models_data = await self.client.list_models()

            models_list = []
            for model_data in models_data:
                model_name = model_data.get("name", "")
                if not model_name:
                    continue

                model_id = model_name.replace("models/", "")
                display_name = model_data.get("display_name", model_id)
                description = model_data.get(
                    "description", f"Google {display_name} model"
                )

                supported_methods = model_data.get("supported_generation_methods", ['generateContent'])

                input_token_limit = model_data.get("input_token_limit", 32768)
                output_token_limit = model_data.get("output_token_limit", 8192)
                context_length = min(input_token_limit, 128000)

                logger.debug(
                    f"Found Gemini model: {model_id}",
                    extra={
                        "display_name": display_name,
                        "supported_methods": supported_methods,
                        "input_token_limit": input_token_limit,
                        "output_token_limit": output_token_limit,
                        "context_length": context_length,
                    },
                )

                pricing_config = Pricing(
                    prompt=0.000003,
                    completion=0.000003,
                    request=0.0,
                    image=0.0,
                    web_search=0.0,
                    internal_reasoning=0.0,
                    max_prompt_cost=0.001,
                    max_completion_cost=0.001,
                    max_cost=0.001,
                )

                models_list.append(
                    Model(
                        id=model_id,
                        name=display_name,
                        created=0,
                        description=description,
                        context_length=context_length,
                        architecture=Architecture(
                            modality="text",
                            input_modalities=["text"],
                            output_modalities=["text"],
                            tokenizer="gemini",
                            instruct_type=None,
                        ),
                        pricing=pricing_config,
                        sats_pricing=None,
                        per_request_limits=None,
                        top_provider=TopProvider(
                            context_length=context_length,
                            max_completion_tokens=output_token_limit,
                            is_moderated=True,
                        ),
                        enabled=True,
                        upstream_provider_id=None,
                        canonical_slug=None,
                    )
                )

            logger.info(
                f"Fetched {len(models_list)} models from Gemini",
                extra={"model_count": len(models_list), "base_url": self.base_url},
            )
            return models_list

        except Exception as e:
            logger.error(
                f"Failed to fetch models from Gemini API: {e}",
                extra={
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "base_url": self.base_url,
                },
            )
            return []

    async def refresh_models_cache(self) -> None:
        try:
            from ..payment.models import _update_model_sats_pricing
            from ..payment.price import sats_usd_price

            models = await self.fetch_models()
            models_with_fees = [self._apply_provider_fee_to_model(m) for m in models]

            try:
                sats_to_usd = sats_usd_price()
                self._models_cache = [
                    _update_model_sats_pricing(m, sats_to_usd) for m in models_with_fees
                ]
            except Exception:
                self._models_cache = models_with_fees

            self._models_by_id = {m.id: m for m in self._models_cache}
            logger.info(
                f"Refreshed models cache for {self.base_url}",
                extra={"model_count": len(models)},
            )
        except Exception as e:
            logger.error(
                f"Failed to refresh models cache for {self.base_url}",
                extra={"error": str(e), "error_type": type(e).__name__},
            )

    def get_cached_models(self) -> list[Model]:
        return self._models_cache

    def get_cached_model_by_id(self, model_id: str) -> Model | None:
        return self._models_by_id.get(model_id)

    def _apply_provider_fee_to_model(self, model: Model) -> Model:
        from ..payment.models import Model, Pricing, _calculate_usd_max_costs

        adjusted_pricing = Pricing.parse_obj(
            {k: v * self.provider_fee for k, v in model.pricing.dict().items()}
        )

        temp_model = Model(
            id=model.id,
            name=model.name,
            created=model.created,
            description=model.description,
            context_length=model.context_length,
            architecture=model.architecture,
            pricing=adjusted_pricing,
            sats_pricing=None,
            per_request_limits=model.per_request_limits,
            top_provider=model.top_provider,
            enabled=model.enabled,
            upstream_provider_id=model.upstream_provider_id,
            canonical_slug=model.canonical_slug,
        )

        (
            adjusted_pricing.max_prompt_cost,
            adjusted_pricing.max_completion_cost,
            adjusted_pricing.max_cost,
        ) = _calculate_usd_max_costs(temp_model)

        return Model(
            id=model.id,
            name=model.name,
            created=model.created,
            description=model.description,
            context_length=model.context_length,
            architecture=model.architecture,
            pricing=adjusted_pricing,
            sats_pricing=model.sats_pricing,
            per_request_limits=model.per_request_limits,
            top_provider=model.top_provider,
            enabled=model.enabled,
            upstream_provider_id=model.upstream_provider_id,
            canonical_slug=model.canonical_slug,
        )
