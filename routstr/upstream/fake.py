import asyncio
import json
import random
from typing import AsyncIterator

from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from ..core.db import ApiKey, AsyncSession
from ..payment.models import Architecture, Model, Pricing
from .base import BaseUpstreamProvider


class MockUpstreamProvider(BaseUpstreamProvider):
    """Fack Mock Upstream provider specifically for Testing."""

    provider_type = "mock"

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
        if path.endswith("chat/completions"):
            is_streaming = False
            if request_body:
                request_data = json.loads(request_body)
                is_streaming = request_data.get("stream", False)

            if is_streaming:

                async def fake_streaming_response(
                    chunk_size: int | None = None,
                ) -> AsyncIterator[bytes]:
                    suffix = random.randint(1000, 9999)
                    req_id = f"gen-mock-stream-{suffix}"
                    created = 1766138895
                    model = "mock/gpt-420-mock"

                    def make_chunk(
                        delta: dict,
                        finish_reason: str | None = None,
                        usage: dict | None = None,
                    ) -> bytes:
                        chunk = {
                            "id": req_id,
                            "provider": "MockProvider",
                            "model": model,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": delta,
                                    "finish_reason": finish_reason,
                                    "native_finish_reason": "completed"
                                    if finish_reason
                                    else None,
                                    "logprobs": None,
                                }
                            ],
                        }
                        if usage:
                            chunk["usage"] = usage
                        return f"data: {json.dumps(chunk)}\n\n".encode()

                    # 1. Initial chunk
                    yield make_chunk({"role": "assistant", "content": ""})
                    await asyncio.sleep(0.02)

                    # 2. Reasoning chunks
                    reasoning_tokens = ["Mock", " reason", "ing", "..."]
                    for token in reasoning_tokens:
                        delta = {
                            "role": "assistant",
                            "content": "",
                            "reasoning": token,
                            "reasoning_details": [
                                {
                                    "type": "reasoning.summary",
                                    "summary": token,
                                    "format": "openai-responses-v1",
                                    "index": 0,
                                }
                            ],
                        }
                        yield make_chunk(delta)
                        await asyncio.sleep(0.03)

                    # 3. Content chunks
                    content_tokens = ["This", " is", " a", " mock", " stream", "."]
                    for token in content_tokens:
                        yield make_chunk({"role": "assistant", "content": token})
                        await asyncio.sleep(0.03)

                    # 4. Finish chunk
                    yield make_chunk(
                        {"role": "assistant", "content": ""}, finish_reason="stop"
                    )

                    # 5. Usage chunk
                    usage_data = {
                        "prompt_tokens": 10,
                        "completion_tokens": 20,
                        "total_tokens": 30,
                        "cost": 0.001,
                        "is_byok": False,
                        "prompt_tokens_details": {
                            "cached_tokens": 0,
                            "audio_tokens": 0,
                            "video_tokens": 0,
                        },
                        "cost_details": {
                            "upstream_inference_cost": None,
                            "upstream_inference_prompt_cost": 0,
                            "upstream_inference_completions_cost": 0.001,
                        },
                        "completion_tokens_details": {
                            "reasoning_tokens": 10,
                            "image_tokens": 0,
                        },
                    }

                    usage_chunk = {
                        "id": req_id,
                        "provider": "MockProvider",
                        "model": model,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": ""},
                                "finish_reason": None,
                                "native_finish_reason": None,
                                "logprobs": None,
                            }
                        ],
                        "usage": usage_data,
                    }
                    yield f"data: {json.dumps(usage_chunk)}\n\n".encode()

                    # 6. DONE
                    yield b"data: [DONE]\n\n"

                    # 7. Cost
                    cost_chunk = {
                        "cost": {
                            "base_msats": 0,
                            "input_msats": 2,
                            "output_msats": 10,
                            "total_msats": 12,
                        }
                    }
                    yield f"data: {json.dumps(cost_chunk)}\n\n".encode()

                return StreamingResponse(
                    fake_streaming_response(),
                    200,
                )

            else:
                suffix = random.randint(1000, 9999)
                content_dict = {
                    "id": f"gen-mock-{suffix}",
                    "provider": "MockProvider",
                    "model": "mock/gpt-5-mini",
                    "object": "chat.completion",
                    "created": 1766138655,
                    "choices": [
                        {
                            "logprobs": None,
                            "finish_reason": "length",
                            "native_finish_reason": "max_output_tokens",
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": f"Mock Content {suffix}",
                                "refusal": None,
                                "reasoning": f"Mock Reasoning {suffix}",
                                "reasoning_details": [
                                    {
                                        "format": "openai-responses-v1",
                                        "index": 0,
                                        "type": "reasoning.summary",
                                        "summary": f"Mock Summary {suffix}",
                                    },
                                    {
                                        "id": f"rs_mock_{suffix}",
                                        "format": "openai-responses-v1",
                                        "index": 0,
                                        "type": "reasoning.encrypted",
                                        "data": "mock_encrypted_data",
                                    },
                                ],
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 10,
                        "total_tokens": 20,
                        "cost": 0,
                        "is_byok": False,
                        "prompt_tokens_details": {
                            "cached_tokens": 0,
                            "audio_tokens": 0,
                            "video_tokens": 0,
                        },
                        "cost_details": {
                            "upstream_inference_cost": None,
                            "upstream_inference_prompt_cost": 0,
                            "upstream_inference_completions_cost": 0,
                        },
                        "completion_tokens_details": {
                            "reasoning_tokens": 5,
                            "image_tokens": 0,
                        },
                    },
                    "cost": {
                        "base_msats": 0,
                        "input_msats": 0,
                        "output_msats": 0,
                        "total_msats": 0,
                    },
                }
                return Response(json.dumps(content_dict).encode(), 200)

        elif path.endswith("embeddings"):
            raise NotImplementedError
        elif path.endswith("responses"):
            raise NotImplementedError
        else:
            raise NotImplementedError

    async def fetch_models(self) -> list[Model]:
        return [
            Model(
                id="mock/gpt-420-mock",
                name="mock/gpt-420-mock",
                created=0,
                description="mock model for testing",
                context_length=8192,
                architecture=Architecture(
                    modality="text",
                    input_modalities=["text"],
                    output_modalities=["text"],
                    tokenizer="",
                    instruct_type=None,
                ),
                pricing=Pricing(prompt=0.01, completion=0.01),
            ),
        ]

    def transform_model_name(self, model_id: str) -> str:
        return "fake-model"

    async def get_balance(self) -> float | None:
        return 420.69
