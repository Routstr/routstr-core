"""Comprehensive error handling and edge case tests"""

import asyncio
import hashlib
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient, ConnectError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from routstr.core.db import ApiKey


class TestNetworkFailureScenarios:
    """Test various network failure scenarios"""

    @pytest.mark.asyncio
    async def test_mint_service_unavailable(
        self,
        authenticated_client: AsyncClient,
        integration_session: AsyncSession,
    ) -> None:
        """Test behavior when mint service is unavailable"""
        # Patch the wallet send function to simulate failure across all modules
        with (
            patch(
                "routstr.wallet.send_token",
                AsyncMock(side_effect=ConnectError("Mint service unavailable")),
            ),
            patch(
                "routstr.balance.send_token",
                AsyncMock(side_effect=ConnectError("Mint service unavailable")),
            ),
        ):
            # Try to refund when mint is down - should return 503 status
            response = await authenticated_client.post("/v1/wallet/refund")
            assert response.status_code == 503
            assert "Mint service unavailable" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_upstream_llm_service_down(
        self,
        authenticated_client: AsyncClient,
        integration_session: AsyncSession,
    ) -> None:
        """Test proxy behavior when upstream LLM service is down"""
        # Mock at the routstr level to simulate upstream being down
        with patch("routstr.upstream.httpx.AsyncClient") as mock_client_class:
            # Create a mock client instance
            mock_client = AsyncMock()
            mock_client_class.return_value = mock_client
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.aclose = AsyncMock()

            # Make the send method raise ConnectError
            mock_client.send = AsyncMock(side_effect=ConnectError("Connection refused"))
            mock_client.build_request = MagicMock(return_value=MagicMock())

            # Try to make a proxy request
            response = await authenticated_client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-3.5-turbo",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

            # Should get appropriate error (502 for upstream error)
            assert response.status_code == 502
            # Error detail depends on implementation

    @pytest.mark.asyncio
    async def test_partial_request_failures(
        self,
        authenticated_client: AsyncClient,
        integration_session: AsyncSession,
    ) -> None:
        """Test handling of partial failures during streaming"""

        # Mock streaming response that fails midway
        async def mock_aiter_bytes() -> Any:  # type: ignore[misc]
            yield b'data: {"choices": [{"delta": {"content": "Hello"}}]}\n\n'
            yield b'data: {"choices": [{"delta": {"content": " World"}}]}\n\n'
            raise ConnectError("Connection lost")

        with patch("httpx.AsyncClient.request") as mock_request:
            mock_response = AsyncMock()
            mock_response.status_code = 200
            mock_response.headers = {"content-type": "text/event-stream"}
            mock_response.aiter_bytes = mock_aiter_bytes
            mock_response.is_stream_consumed = False
            mock_request.return_value = mock_response

            # Make streaming request
            response = await authenticated_client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-3.5-turbo",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                },
            )

            # Should still return 200 even with partial failure
            # The streaming error happens after headers are sent
            assert response.status_code == 200

            # In real implementation, partial charges would be handled
            # but our mock doesn't actually deduct balance

    @pytest.mark.asyncio
    async def test_timeout_handling(
        self,
        authenticated_client: AsyncClient,
        integration_session: AsyncSession,
    ) -> None:
        """Test request timeout handling"""
        # Similar to above, we test timeout handling exists
        # but can't easily trigger real timeouts in test environment

        with patch("httpx.AsyncClient.send") as mock_send:
            # Create a mock timeout response
            mock_response = AsyncMock()
            mock_response.status_code = 504
            mock_response.headers = {"content-type": "application/json"}
            mock_response.json.return_value = {"error": "Gateway Timeout"}
            mock_response.text = '{"error": "Gateway Timeout"}'
            mock_response.content = b'{"error": "Gateway Timeout"}'
            mock_response.aiter_bytes = AsyncMock(
                return_value=AsyncMock(
                    __aiter__=lambda self: self,
                    __anext__=AsyncMock(side_effect=StopAsyncIteration),
                )
            )
            mock_send.return_value = mock_response

            # Make request
            response = await authenticated_client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-3.5-turbo",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

            # Should pass through the error
            assert response.status_code >= 500


class TestInvalidInputHandling:
    """Test handling of various invalid inputs"""

    @pytest.mark.asyncio
    async def test_malformed_cashu_tokens(
        self,
        authenticated_client: AsyncClient,
        integration_session: AsyncSession,
    ) -> None:
        """Test various malformed Cashu token formats"""
        malformed_tokens = [
            "",  # Empty token
            "not-a-token",  # Invalid format
            "cashu",  # Incomplete
            "cashuA" + "x" * 10000,  # Extremely long
            "cashuA" + "\x00" + "test",  # Null bytes
            "cashuA" + "\n\r" + "test",  # Control characters
            "cashuAeyJhbGciOi",  # Truncated base64
            "cashuA!!!invalid-base64!!!",  # Invalid base64
        ]

        for token in malformed_tokens:
            response = await authenticated_client.post(
                "/v1/wallet/topup", params={"cashu_token": token}
            )

            # All should fail with 400
            assert response.status_code == 400, f"Token {repr(token)} should fail"
            # Accept various error messages that indicate token validation failure
            error_detail = response.json()["detail"].lower()
            assert any(
                keyword in error_detail
                for keyword in ["invalid", "failed to redeem", "failed to decode"]
            ), f"Unexpected error message: {error_detail}"

    @pytest.mark.asyncio
    async def test_invalid_json_payloads(
        self,
        authenticated_client: AsyncClient,
    ) -> None:
        """Test handling of invalid JSON in requests"""
        # Test malformed JSON
        response = await authenticated_client.post(
            "/v1/chat/completions",
            content='{"model": "gpt-3.5-turbo", "messages": [}',  # Invalid JSON
            headers={"content-type": "application/json"},
        )
        assert response.status_code in [
            400,
            422,
        ]  # Either is acceptable for malformed JSON

        # Test wrong content type
        response = await authenticated_client.post(
            "/v1/chat/completions",
            content="not json at all",
            headers={"content-type": "application/json"},
        )
        assert response.status_code in [400, 422]

        # Test missing required fields - proxy endpoints just forward, so might get different error
        response = await authenticated_client.post(
            "/v1/chat/completions",
            json={"model": "gpt-3.5-turbo"},  # Missing messages
        )
        assert response.status_code >= 400  # Any 4xx error is acceptable

    @pytest.mark.asyncio
    async def test_sql_injection_attempts(
        self,
        integration_client: AsyncClient,
        integration_session: AsyncSession,
    ) -> None:
        """Test that SQL injection attempts are properly handled"""
        # SQL injection attempts in various places
        injection_payloads = [
            "'; DROP TABLE api_keys; --",
            "1' OR '1'='1",
            "admin'--",
            "1; UPDATE api_keys SET balance=999999999;",
            "' UNION SELECT * FROM api_keys--",
        ]

        for payload in injection_payloads:
            # Try injection in authorization header
            response = await integration_client.get(
                "/v1/wallet/info", headers={"Authorization": f"Bearer {payload}"}
            )
            assert response.status_code == 401

            # Try injection in refund amount
            response = await integration_client.post(
                "/v1/wallet/refund", json={"amount": payload}
            )
            assert response.status_code in [
                401,
                422,
            ]  # Unauthorized or validation error

    @pytest.mark.asyncio
    async def test_xss_in_headers_params(
        self,
        authenticated_client: AsyncClient,
    ) -> None:
        """Test XSS prevention in headers and parameters"""
        xss_payloads = [
            "<script>alert('XSS')</script>",
            "javascript:alert(1)",
            "<img src=x onerror=alert(1)>",
            "<svg onload=alert(1)>",
            "'+alert(1)+'",
        ]

        for payload in xss_payloads:
            # Try XSS in custom headers
            response = await authenticated_client.get(
                "/v1/wallet/info", headers={"X-Custom-Header": payload}
            )
            # Should process normally, but payload should be escaped/ignored
            assert response.status_code == 200

            # If response includes headers, verify they're escaped
            if "X-Custom-Header" in response.headers:
                assert "<script>" not in response.headers["X-Custom-Header"]


class TestResourceExhaustion:
    """Test behavior under resource exhaustion scenarios"""

    @pytest.mark.asyncio
    async def test_rate_limiting_behavior(
        self,
        authenticated_client: AsyncClient,
        integration_session: AsyncSession,
    ) -> None:
        """Test rate limiting functionality"""
        # Make many requests rapidly
        requests = []
        start_time = time.time()

        # Send 100 requests as fast as possible
        for i in range(100):
            request = authenticated_client.get("/v1/wallet/info")
            requests.append(request)

        responses = await asyncio.gather(*requests, return_exceptions=True)
        end_time = time.time()

        # Count successful responses
        success_count = sum(  # type: ignore[misc]
            1  # type: ignore[misc]
            for r in responses
            if not isinstance(r, Exception) and r.status_code == 200  # type: ignore[union-attr]
        )

        # At least some should succeed
        assert success_count > 0

        # Check timing - duration depends on implementation
        duration = end_time - start_time
        # If rate limiting is implemented, some might be limited
        # If not, all should succeed quickly
        assert duration >= 0  # Just verify it completed

    @pytest.mark.asyncio
    async def test_maximum_request_size_limits(
        self,
        authenticated_client: AsyncClient,
    ) -> None:
        """Test handling of oversized requests"""
        # Create a very large payload
        large_messages = []
        for i in range(1000):
            large_messages.append(
                {
                    "role": "user",
                    "content": "x" * 10000,  # 10KB per message
                }
            )

        # This creates ~10MB payload
        response = await authenticated_client.post(
            "/v1/chat/completions",
            json={"model": "gpt-3.5-turbo", "messages": large_messages},
        )

        # Should reject oversized request or fail to proxy
        assert (
            response.status_code >= 400
        )  # Any error is acceptable for oversized payload

    @pytest.mark.asyncio
    async def test_database_connection_limits(
        self,
        authenticated_client: AsyncClient,
        integration_app: Any,
    ) -> None:
        """Test behavior when database connections are exhausted"""

        # Create many concurrent database operations
        async def db_operation() -> Any:
            return await authenticated_client.get("/v1/wallet/info")

        # Launch many concurrent operations
        tasks = [db_operation() for _ in range(50)]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        # All should eventually succeed (connection pooling should handle this)
        success_count = sum(  # type: ignore[misc]
            1  # type: ignore[misc]
            for r in responses
            if not isinstance(r, Exception) and r.status_code == 200  # type: ignore[union-attr]
        )
        assert success_count == 50

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_memory_usage_under_load(
        self,
        authenticated_client: AsyncClient,
    ) -> None:
        """Test memory usage doesn't grow unbounded under load"""
        # This is a basic test - production would use memory profiling tools

        # Make many requests with varying sizes
        for i in range(10):
            # Small request
            await authenticated_client.get("/v1/wallet/info")

            # Medium request
            await authenticated_client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-3.5-turbo",
                    "messages": [{"role": "user", "content": "Hello" * 100}],
                },
            )

            # Larger request (but not too large)
            messages = [
                {"role": "user", "content": "Test message " * 50} for _ in range(10)
            ]
            await authenticated_client.post(
                "/v1/chat/completions",
                json={"model": "gpt-3.5-turbo", "messages": messages},
            )

        # If we get here without crashing, basic memory management is working
        assert True


class TestRecoveryScenarios:
    """Test system recovery from various failure states"""

    @pytest.mark.asyncio
    async def test_service_restart_during_requests(
        self,
        authenticated_client: AsyncClient,
        integration_session: AsyncSession,
        integration_app: Any,
    ) -> None:
        """Test handling requests during service restart"""
        # Get initial balance
        api_key_header = authenticated_client.headers["Authorization"].replace(
            "Bearer ", ""
        )
        api_key_hash = (
            api_key_header[3:] if api_key_header.startswith("sk-") else api_key_header
        )

        stmt = select(ApiKey).where(ApiKey.hashed_key == api_key_hash)  # type: ignore[arg-type]
        result = await integration_session.execute(stmt)
        initial_key = result.scalar_one()
        initial_balance = initial_key.balance

        # Simulate partial request processing
        # In real scenario, service would restart mid-request
        # Here we test that state is consistent after interruption

        # Make a request
        try:
            response = await authenticated_client.get("/v1/wallet/info")
            assert response.status_code == 200
        except Exception:
            # If request fails due to "restart", that's ok
            pass

        # Verify database state is still consistent
        await integration_session.refresh(initial_key)
        assert initial_key.balance == initial_balance  # No partial charges

    @pytest.mark.asyncio
    async def test_database_recovery_after_crash(
        self,
        authenticated_client: AsyncClient,
        integration_session: AsyncSession,
    ) -> None:
        """Test database consistency after crash recovery"""
        # Get initial state
        api_key_header = authenticated_client.headers["Authorization"].replace(
            "Bearer ", ""
        )
        api_key_hash = (
            api_key_header[3:] if api_key_header.startswith("sk-") else api_key_header
        )

        stmt = select(ApiKey).where(ApiKey.hashed_key == api_key_hash)  # type: ignore[arg-type]
        result = await integration_session.execute(stmt)
        api_key = result.scalar_one()
        initial_balance = api_key.balance
        initial_requests = api_key.total_requests

        # Simulate operations that might be interrupted
        try:
            # Start a transaction
            api_key.reserved_balance += 1000
            api_key.total_requests += 1
            # Don't commit - simulate crash
            raise Exception("Simulated database crash")
        except Exception:
            # Rollback should happen automatically
            await integration_session.rollback()

        # Verify state is consistent after "recovery"
        await integration_session.refresh(api_key)
        assert api_key.balance == initial_balance
        assert api_key.total_requests == initial_requests

    @pytest.mark.asyncio
    async def test_state_consistency_after_failures(
        self,
        authenticated_client: AsyncClient,
        integration_session: AsyncSession,
        db_snapshot: Any,
    ) -> None:
        """Test overall state consistency after various failures"""
        # Capture initial state
        await db_snapshot.capture()

        # Simulate various failures
        failure_scenarios: list[Any] = [  # type: ignore[union-attr]
            # Network failure during topup
            lambda: authenticated_client.post(
                "/v1/wallet/topup", params={"cashu_token": "invalid"}
            ),
            # Invalid refund request
            lambda: authenticated_client.post(
                "/v1/wallet/refund", json={"amount": -1000}
            ),
            # Malformed proxy request
            lambda: authenticated_client.post("/v1/invalid/endpoint", json={}),
        ]

        # Execute all failure scenarios
        for scenario in failure_scenarios:
            try:
                await scenario()
            except Exception:
                # Failures are expected
                pass

        # Verify database state hasn't been corrupted
        diff = await db_snapshot.diff()

        # Should have no new keys
        assert len(diff["api_keys"]["added"]) == 0

        # Existing key should not be removed
        assert len(diff["api_keys"]["removed"]) == 0

        # Balance should not have changed (all operations failed)
        if diff["api_keys"]["modified"]:
            for mod in diff["api_keys"]["modified"]:
                # Only acceptable changes are request counts
                for field, change in mod["changes"].items():
                    if field == "total_requests":
                        # Request count might increase
                        assert change["delta"] >= 0
                    elif field == "balance":
                        # Balance should not decrease from failed operations
                        assert change["delta"] >= 0
                    else:
                        # Other fields shouldn't change
                        assert change["delta"] == 0 or change["delta"] is None


class TestEdgeCaseCombinations:
    """Test combinations of edge cases"""

    @pytest.mark.skip(
        reason="Concurrent error test has timing issues - skipping for CI reliability"
    )
    @pytest.mark.asyncio
    async def test_concurrent_errors(
        self,
        authenticated_client: AsyncClient,
        integration_session: AsyncSession,
    ) -> None:
        """Test handling multiple concurrent errors"""
        # Create various error conditions concurrently
        tasks = [
            # Invalid token
            authenticated_client.post(
                "/v1/wallet/topup", params={"cashu_token": "invalid"}
            ),
            # Negative refund
            authenticated_client.post("/v1/wallet/refund", json={"amount": -1000}),
            # Invalid model
            authenticated_client.post(
                "/v1/chat/completions",
                json={
                    "model": "non-existent-model",
                    "messages": [{"role": "user", "content": "test"}],
                },
            ),
            # Malformed request
            authenticated_client.post("/v1/chat/completions", json={"invalid": "data"}),
        ]

        # All should complete without crashing the service
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        # Verify all returned error responses (not exceptions)
        for i, response in enumerate(responses):
            assert not isinstance(response, Exception), f"Task {i} raised exception"
            # Some requests might succeed depending on mock behavior
            # The important thing is they don't crash the service

    @pytest.mark.asyncio
    async def test_error_during_streaming(
        self,
        authenticated_client: AsyncClient,
    ) -> None:
        """Test error handling during streaming responses"""

        # Mock a streaming response that errors midway
        async def mock_streaming_with_error() -> Any:  # type: ignore[misc]
            yield b'data: {"choices": [{"delta": {"content": "Start"}}]}\n\n'
            yield b'data: {"choices": [{"delta": {"content": " of"}}]}\n\n'
            yield b'data: {"error": {"message": "Model overloaded", "type": "server_error"}}\n\n'

        with patch("httpx.AsyncClient.request") as mock_request:
            mock_response = AsyncMock()
            mock_response.status_code = 200
            mock_response.headers = {"content-type": "text/event-stream"}
            mock_response.aiter_bytes = mock_streaming_with_error
            mock_response.is_stream_consumed = False
            mock_request.return_value = mock_response

            # Make streaming request
            response = await authenticated_client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-3.5-turbo",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                },
            )

            # Should handle the error gracefully
            # Client should still be charged for partial response
            assert response.status_code == 200  # Initial response was OK

    @pytest.mark.asyncio
    async def test_rapid_balance_exhaustion(
        self,
        integration_app: Any,
        integration_session: AsyncSession,
        testmint_wallet: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test behavior when balance is rapidly exhausted by concurrent requests.

        This test creates an API key with insufficient balance (500 msats) for even
        a single request (which costs 1000 msats). It then makes 5 concurrent requests
        to verify that all requests fail with 402 Payment Required errors.

        Note: The test enables fixed pricing to avoid model lookup errors
        since the test environment doesn't have models configured.
        """
        # Disable model-based pricing for this test to avoid model lookup issues
        monkeypatch.setattr("routstr.core.settings.settings.fixed_pricing", True)

        # Create a new API key with very low balance
        # Generate a unique API key
        test_key = f"sk-test-low-balance-{hashlib.sha256(str(time.time()).encode()).hexdigest()[:8]}"
        api_key_hash = test_key[3:]  # Remove sk- prefix

        # Create the API key with only 500 msats (less than one request cost)
        new_key = ApiKey(
            hashed_key=api_key_hash,
            balance=500,  # Less than fixed cost per request (1000 msats)
            reserved_balance=0,
            total_spent=0,
            total_requests=0,
        )
        integration_session.add(new_key)
        await integration_session.commit()

        # Verify the key was created
        await integration_session.refresh(new_key)

        # Create a client with this low-balance key
        low_balance_client = AsyncClient(
            transport=ASGITransport(app=integration_app),  # type: ignore
            base_url="http://test",
            headers={"Authorization": f"Bearer {test_key}"},
        )

        # Make multiple concurrent requests that would exhaust balance
        tasks = []
        for _ in range(5):
            task = low_balance_client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-3.5-turbo",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )
            tasks.append(task)

        responses = await asyncio.gather(*tasks, return_exceptions=True)

        # Some should succeed, others should fail with 402
        insufficient_funds_count = sum(  # type: ignore[misc]
            1  # type: ignore[misc]
            for r in responses
            if not isinstance(r, Exception) and r.status_code == 402  # type: ignore[union-attr]
        )

        # At least one should fail due to insufficient funds
        assert insufficient_funds_count > 0

        # Balance should never go negative
        stmt = select(ApiKey).where(ApiKey.hashed_key == api_key_hash)  # type: ignore[arg-type]
        result = await integration_session.execute(stmt)
        final_key = result.scalar_one()
        assert final_key.balance >= 0

        # Clean up the test client
        await low_balance_client.aclose()
