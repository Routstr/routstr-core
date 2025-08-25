import asyncio
import hashlib
import json
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from routstr.core.db import ApiKey


class CashuTokenGenerator:
    """Utility for generating valid test Cashu tokens"""

    @staticmethod
    def generate_token(
        amount: int,
        mint_url: str = "https://testmint.routstr.com",
        memo: Optional[str] = None,
    ) -> str:
        """Generate a valid Cashu token for testing"""
        import base64
        import secrets

        proofs = []
        remaining = amount

        # Use standard Cashu denominations
        denominations = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192]
        denominations.reverse()  # Start with largest

        for denom in denominations:
            while remaining >= denom:
                proofs.append(
                    {
                        "id": secrets.token_hex(16),
                        "amount": denom,
                        "secret": secrets.token_hex(32),
                        "C": secrets.token_hex(33),
                    }
                )
                remaining -= denom

        token_data = {
            "token": [{"mint": mint_url, "proofs": proofs}],
            "unit": "sat",
            "memo": memo or f"Test token {amount} sats",
        }

        token_json = json.dumps(token_data)
        token_base64 = base64.urlsafe_b64encode(token_json.encode()).decode()
        return f"cashuA{token_base64}"

    @staticmethod
    def generate_invalid_token() -> str:
        """Generate various types of invalid tokens for testing"""
        import base64
        import random

        invalid_types: List[Callable[[], str]] = [
            # Malformed base64
            lambda: "cashuA" + "invalid-base64!@#",
            # Missing cashuA prefix
            lambda: base64.urlsafe_b64encode(b'{"token": []}').decode(),
            # Invalid JSON structure
            lambda: "cashuA"
            + base64.urlsafe_b64encode(b'{"invalid": "structure"}').decode(),
            # Invalid proof structure
            lambda: CashuTokenGenerator._encode_token(
                {
                    "token": [
                        {"mint": "https://test.com", "proofs": [{"invalid": "proof"}]}
                    ],
                    "unit": "sat",
                }
            ),
        ]

        return random.choice(invalid_types)()

    @staticmethod
    def _encode_token(data: Dict[str, Any]) -> str:
        """Helper to encode token data"""
        import base64

        token_json = json.dumps(data)
        token_base64 = base64.urlsafe_b64encode(token_json.encode()).decode()
        return f"cashuA{token_base64}"


class DatabaseStateValidator:
    """Utilities for validating database state in tests"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_api_key(self, api_key: str) -> Optional[ApiKey]:
        """Get API key from database"""
        hashed_key = hashlib.sha256(api_key.encode()).hexdigest()
        result = await self.session.execute(
            select(ApiKey).where(ApiKey.hashed_key == hashed_key)  # type: ignore[arg-type]
        )
        return result.scalar_one_or_none()

    async def validate_balance_change(
        self, api_key: str, expected_balance: int, tolerance: int = 0
    ) -> Dict[str, Any]:
        """Validate that balance matches expected amount within tolerance"""
        key_obj = await self.get_api_key(api_key)
        if not key_obj:
            return {"valid": False, "error": "API key not found"}

        actual_balance = key_obj.balance
        difference = abs(actual_balance - expected_balance)

        return {
            "valid": difference <= tolerance,
            "expected_balance": expected_balance,
            "actual_balance": actual_balance,
            "difference": difference,
            "tolerance": tolerance,
            "current_balance": key_obj.balance,
        }

    async def validate_request_count(
        self, api_key: str, expected_count: int
    ) -> Dict[str, Any]:
        """Validate request count for an API key"""
        key_obj = await self.get_api_key(api_key)
        if not key_obj:
            return {"valid": False, "error": "API key not found"}

        return {
            "valid": key_obj.total_requests == expected_count,
            "expected": expected_count,
            "actual": key_obj.total_requests,
        }

    async def validate_atomic_update(
        self, api_key: str, field: str, expected_value: Any
    ) -> bool:
        """Validate that a field was updated atomically"""
        key_obj = await self.get_api_key(api_key)
        if not key_obj:
            return False

        actual_value = getattr(key_obj, field)
        return actual_value == expected_value


class ResponseValidator:
    """Utilities for validating API responses"""

    @staticmethod
    def validate_error_response(
        response: httpx.Response,
        expected_status: int,
        expected_error_key: str = "detail",
    ) -> Dict[str, Any]:
        """Validate error response format"""
        is_valid = response.status_code == expected_status

        result: Dict[str, Any] = {
            "valid": is_valid,
            "status_code": response.status_code,
            "expected_status": expected_status,
        }

        try:
            error_data = response.json()
            has_error_key = expected_error_key in error_data
            result["has_error_key"] = has_error_key
            result["error_message"] = error_data.get(expected_error_key)
            result["valid"] = is_valid and has_error_key
        except json.JSONDecodeError:
            result["valid"] = False
            result["error"] = "Invalid JSON response"

        return result

    @staticmethod
    def validate_success_response(
        response: httpx.Response,
        expected_status: int = 200,
        required_fields: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Validate successful response format"""
        is_valid = response.status_code == expected_status

        result: Dict[str, Any] = {
            "valid": is_valid,
            "status_code": response.status_code,
            "expected_status": expected_status,
        }

        if required_fields:
            try:
                data = response.json()
                missing_fields = [
                    field for field in required_fields if field not in data
                ]
                result["missing_fields"] = missing_fields
                result["valid"] = is_valid and len(missing_fields) == 0
            except json.JSONDecodeError:
                result["valid"] = False
                result["error"] = "Invalid JSON response"

        return result

    @staticmethod
    def validate_streaming_response(
        chunks: List[bytes],
        expected_format: str = "sse",  # Server-Sent Events
    ) -> Dict[str, Any]:
        """Validate streaming response format"""
        result: Dict[str, Any] = {
            "valid": True,
            "chunk_count": len(chunks),
            "total_bytes": sum(len(chunk) for chunk in chunks),
        }

        if expected_format == "sse":
            # Validate SSE format
            events: List[Any] = []
            for chunk in chunks:
                chunk_str = chunk.decode("utf-8")
                if chunk_str.startswith("data: "):
                    try:
                        event_data = json.loads(chunk_str[6:])
                        events.append(event_data)
                    except json.JSONDecodeError:
                        result["valid"] = False
                        result["error"] = f"Invalid JSON in SSE chunk: {chunk_str}"

            result["events"] = events
            result["event_count"] = len(events)

        return result


class PerformanceValidator:
    """Utilities for validating performance requirements"""

    def __init__(self) -> None:
        self.measurements: Dict[str, List[float]] = {}

    def start_timing(self, operation: str) -> float:
        """Start timing an operation"""
        return time.time()

    def end_timing(self, operation: str, start_time: float) -> float:
        """End timing and record the duration"""
        duration = time.time() - start_time

        if operation not in self.measurements:
            self.measurements[operation] = []

        self.measurements[operation].append(duration)
        return duration

    def validate_response_time(
        self, operation: str, max_duration: float, percentile: float = 0.95
    ) -> Dict[str, Any]:
        """Validate that response times meet requirements"""
        if operation not in self.measurements:
            return {"valid": False, "error": "No measurements for operation"}

        times = sorted(self.measurements[operation])
        percentile_index = int(len(times) * percentile)
        percentile_time = (
            times[percentile_index] if percentile_index < len(times) else times[-1]
        )

        return {
            "valid": percentile_time <= max_duration,
            "percentile": percentile,
            "percentile_time": percentile_time,
            "max_allowed": max_duration,
            "mean_time": sum(times) / len(times),
            "min_time": min(times),
            "max_time": max(times),
            "sample_count": len(times),
        }


class ConcurrencyTester:
    """Utilities for testing concurrent operations"""

    @staticmethod
    async def run_concurrent_requests(
        client: httpx.AsyncClient,
        requests: List[Dict[str, Any]],
        max_concurrent: int = 10,
    ) -> List[httpx.Response]:
        """Run multiple requests concurrently"""
        semaphore = asyncio.Semaphore(max_concurrent)

        async def make_request(request_data: Dict[str, Any]) -> httpx.Response:
            async with semaphore:
                method = request_data.get("method", "GET")
                url = request_data["url"]
                headers = request_data.get("headers", {})
                json_data = request_data.get("json")
                params = request_data.get("params")

                return await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=json_data,
                    params=params,
                )

        tasks = [make_request(req) for req in requests]
        return await asyncio.gather(*tasks, return_exceptions=False)

    @staticmethod
    async def test_race_condition(
        test_func: Callable[[], Awaitable[Any]],
        iterations: int = 100,
        concurrent_tasks: int = 10,
    ) -> Dict[str, Any]:
        """Test for race conditions by running a function concurrently"""
        results: List[Any] = []
        errors: List[str] = []

        async def wrapped_test() -> Any:
            try:
                result = await test_func()
                results.append(result)
                return result
            except Exception as e:
                errors.append(str(e))
                raise

        # Run tests in batches
        for _ in range(iterations // concurrent_tasks):
            tasks = [wrapped_test() for _ in range(concurrent_tasks)]
            await asyncio.gather(*tasks, return_exceptions=True)

        return {
            "total_runs": iterations,
            "successful_runs": len(results),
            "errors": errors,
            "error_rate": len(errors) / iterations if iterations > 0 else 0,
        }


class MockServiceBuilder:
    """Builder for creating mock services for integration tests"""

    @staticmethod
    def create_mock_llm_response(
        model: str = "gpt-3.5-turbo",
        messages: Optional[List[Dict[str, str]]] = None,
        stream: bool = False,
    ) -> Union[Dict[str, Any], List[str]]:
        """Create a mock LLM API response"""
        if stream:
            # Return SSE formatted chunks
            chunks = []
            response_id = f"chatcmpl-{int(time.time())}"

            # Initial chunk
            chunks.append(
                json.dumps(
                    {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": ""},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
            )

            # Content chunks
            content = "This is a test response from the mock LLM."
            for word in content.split():
                chunks.append(
                    json.dumps(
                        {
                            "id": response_id,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": word + " "},
                                    "finish_reason": None,
                                }
                            ],
                        }
                    )
                )

            # Final chunk
            chunks.append(
                json.dumps(
                    {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                )
            )

            return [f"data: {chunk}\n\n" for chunk in chunks] + ["data: [DONE]\n\n"]

        else:
            # Non-streaming response
            return {
                "id": f"chatcmpl-{int(time.time())}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "This is a test response from the mock LLM.",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                },
            }

    @staticmethod
    def create_mock_error_response(
        status_code: int, error_type: str = "api_error", message: str = "Mock error"
    ) -> Dict[str, Any]:
        """Create a mock error response"""
        return {"error": {"type": error_type, "message": message, "code": status_code}}


class TestDataBuilder:
    """Builder for creating test data"""

    @staticmethod
    def create_api_key_data(
        balance: int = 10000,
        refund_address: Optional[str] = None,
        expiry_hours: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Create test API key data"""
        data: Dict[str, Any] = {
            "balance": balance,
            "total_spent": 0,
            "total_requests": 0,
        }

        if refund_address:
            data["refund_address"] = refund_address

        if expiry_hours:
            expiry_time = datetime.utcnow() + timedelta(hours=expiry_hours)
            data["key_expiry_time"] = int(expiry_time.timestamp())

        return data
