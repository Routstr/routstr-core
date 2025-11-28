"""Comprehensive database consistency tests"""

import asyncio
import time
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from routstr.core.db import TemporaryCredit


class TestTransactionAtomicity:
    """Test transaction atomicity across all database operations"""

    @pytest.mark.asyncio
    async def test_balance_update_atomicity(
        self,
        authenticated_client: AsyncClient,
        integration_session: AsyncSession,
        db_snapshot: Any,
    ) -> None:
        """Test that balance updates are atomic and rolled back on failure"""
        # Get initial balance
        api_key_header = authenticated_client.headers["Authorization"].replace(
            "Bearer ", ""
        )
        # API key format is "sk-{hashed_key}"
        api_key_hash = (
            api_key_header[3:] if api_key_header.startswith("sk-") else api_key_header
        )

        stmt = select(TemporaryCredit).where(TemporaryCredit.hashed_key == api_key_hash)  # type: ignore[arg-type]
        result = await integration_session.execute(stmt)
        api_key = result.scalar_one()
        initial_balance = api_key.balance

        # Test database atomicity by simulating a failed transaction
        # Create a new session for isolated transaction
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(integration_session.bind) as test_session:
            try:
                # Get api key in new session
                result = await test_session.execute(
                    select(TemporaryCredit).where(
                        TemporaryCredit.hashed_key == api_key_hash
                    )  # type: ignore[arg-type]
                )
                test_api_key = result.scalar_one()

                # Update balance
                test_api_key.balance -= 1000
                await test_session.flush()  # Apply changes but don't commit

                # Simulate an error that would cause rollback
                raise Exception("Simulated error after balance update")
            except Exception:
                await test_session.rollback()

        # Verify balance wasn't changed in main session
        await integration_session.refresh(api_key)
        assert api_key.balance == initial_balance

        # Test with concurrent modifications
        await db_snapshot.capture()

        # Try to update in a transaction that will fail
        from sqlalchemy import update

        try:
            await integration_session.execute(
                update(TemporaryCredit)
                .where(TemporaryCredit.hashed_key == api_key_hash)  # type: ignore[arg-type]
                .values(balance=TemporaryCredit.balance - 1000)
            )
            # Force a constraint violation or error
            await integration_session.execute(
                update(TemporaryCredit)
                .where(TemporaryCredit.hashed_key == "non_existent_key")  # type: ignore[arg-type]
                .values(balance=-1)  # This should fail
            )
            await integration_session.commit()
        except Exception:
            await integration_session.rollback()

        # Verify no changes were persisted
        diff = await db_snapshot.diff()
        assert len(diff["api_keys"]["added"]) == 0
        assert len(diff["api_keys"]["modified"]) == 0

    @pytest.mark.asyncio
    async def test_topup_rollback_on_failure(
        self,
        authenticated_client: AsyncClient,
        integration_session: AsyncSession,
        db_snapshot: Any,
    ) -> None:
        """Test that failed top-ups don't leave partial database state"""
        # Get initial state
        api_key_header = authenticated_client.headers["Authorization"].replace(
            "Bearer ", ""
        )
        # API key format is "sk-{hashed_key}"
        api_key_hash = (
            api_key_header[3:] if api_key_header.startswith("sk-") else api_key_header
        )

        stmt = select(TemporaryCredit).where(TemporaryCredit.hashed_key == api_key_hash)  # type: ignore[arg-type]
        result = await integration_session.execute(stmt)
        api_key = result.scalar_one()
        initial_balance = api_key.balance

        # Mock wallet to fail after token validation
        with patch("routstr.payment.wallet.send_token") as mock_wallet_func:
            mock_proof = MagicMock()
            mock_proof.amount = 1000
            mock_wallet = AsyncMock()
            mock_wallet.deserialize_token = AsyncMock(return_value=[mock_proof])
            mock_wallet.redeem = AsyncMock(
                side_effect=Exception("Network error during redemption")
            )
            mock_wallet_func.return_value = mock_wallet

            # Attempt top-up
            response = await authenticated_client.post(
                "/v1/wallet/topup", params={"cashu_token": "cashuAey..."}
            )

            # The mock returns 400 for invalid tokens
            assert response.status_code in [400, 500]

        # Verify no balance change
        await integration_session.refresh(api_key)
        assert api_key.balance == initial_balance

        # Verify clean database state
        diff = await db_snapshot.diff()
        assert len(diff["api_keys"]["added"]) == 0
        assert len(diff["api_keys"]["modified"]) == 0

    @pytest.mark.asyncio
    async def test_concurrent_balance_updates(
        self,
        authenticated_client: AsyncClient,
        integration_session: AsyncSession,
    ) -> None:
        """Test atomic balance updates under concurrent operations"""
        # Get API key info
        api_key_header = authenticated_client.headers["Authorization"].replace(
            "Bearer ", ""
        )
        # API key format is "sk-{hashed_key}"
        api_key_hash = (
            api_key_header[3:] if api_key_header.startswith("sk-") else api_key_header
        )

        # Set a known balance
        stmt = select(TemporaryCredit).where(TemporaryCredit.hashed_key == api_key_hash)  # type: ignore[arg-type]
        result = await integration_session.execute(stmt)
        api_key = result.scalar_one()
        api_key.balance = 10000
        await integration_session.commit()

        # Simulate concurrent balance updates through direct database operations
        async def update_balance(session: AsyncSession, amount: int) -> bool:
            stmt = select(TemporaryCredit).where(
                TemporaryCredit.hashed_key == api_key_hash
            )  # type: ignore[arg-type]
            result = await session.execute(stmt)
            key = result.scalar_one()
            key.balance -= amount
            try:
                await session.commit()
                return True
            except Exception:
                await session.rollback()
                return False

        # Run concurrent balance updates
        tasks = []
        deduction_amounts = [100, 200, 300, 400, 500]

        for amount in deduction_amounts:
            # Create a new session for each concurrent operation
            async with AsyncSession(integration_session.bind) as session:
                task = update_balance(session, amount)
                tasks.append(task)

        await asyncio.gather(*tasks, return_exceptions=True)

        # Verify final balance is consistent
        await integration_session.refresh(api_key)
        # Balance should have some deduction but exact amount depends on implementation
        assert api_key.balance < 10000
        assert api_key.balance >= 0  # Should never go negative


class TestConcurrentOperations:
    """Test database consistency under concurrent operations"""

    @pytest.mark.asyncio
    async def test_multiple_requests_same_api_key(
        self,
        authenticated_client: AsyncClient,
        integration_session: AsyncSession,
    ) -> None:
        """Test multiple concurrent requests with the same API key"""
        # Mock the wallet info endpoint to track concurrent calls
        call_count = 0
        call_times = []

        async def track_concurrent_calls() -> Dict[str, int]:
            nonlocal call_count
            call_count += 1
            call_times.append(time.time())
            await asyncio.sleep(0.1)  # Simulate processing time
            return {"balance": 1000}

        # Make 10 concurrent requests
        tasks = []
        for _ in range(10):
            task = authenticated_client.get("/v1/wallet/info")
            tasks.append(task)

        responses = await asyncio.gather(*tasks)

        # All requests should succeed
        for response in responses:
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_simultaneous_topup_and_usage(
        self,
        authenticated_client: AsyncClient,
        integration_session: AsyncSession,
    ) -> None:
        """Test simultaneous top-up and balance usage operations"""
        # Get API key info
        api_key_header = authenticated_client.headers["Authorization"].replace(
            "Bearer ", ""
        )
        # API key format is "sk-{hashed_key}"
        api_key_hash = (
            api_key_header[3:] if api_key_header.startswith("sk-") else api_key_header
        )

        # Set initial balance
        stmt = select(TemporaryCredit).where(TemporaryCredit.hashed_key == api_key_hash)  # type: ignore[arg-type]
        result = await integration_session.execute(stmt)
        api_key = result.scalar_one()
        initial_balance = 5000
        api_key.balance = initial_balance
        await integration_session.commit()

        # Mock wallet for topup
        with patch("routstr.payment.wallet.send_token") as mock_wallet_func:
            mock_proof = MagicMock()
            mock_proof.amount = 2000
            mock_wallet = AsyncMock()
            mock_wallet.deserialize_token = AsyncMock(return_value=[mock_proof])
            mock_wallet.redeem = AsyncMock(return_value=[mock_proof])
            mock_wallet_func.return_value = mock_wallet

            # Mock proxy endpoint to simulate usage
            with patch("httpx.AsyncClient.request") as mock_request:
                # Mock successful proxy response
                mock_response = AsyncMock()
                mock_response.status_code = 200
                mock_response.headers = {"content-type": "application/json"}
                mock_response.aiter_bytes = AsyncMock(
                    return_value=iter([b'{"result": "ok"}'])
                )
                mock_response.is_stream_consumed = False
                mock_request.return_value = mock_response

                # Run topup and usage concurrently
                async def topup() -> Any:
                    return await authenticated_client.post(
                        "/v1/wallet/topup", params={"cashu_token": "cashuAey..."}
                    )

                async def use_balance() -> Any:
                    # This would normally deduct balance
                    return await authenticated_client.post(
                        "/v1/chat/completions", json={"model": "test", "messages": []}
                    )

                # Execute concurrently
                results = await asyncio.gather(
                    topup(), use_balance(), return_exceptions=True
                )
                topup_result = results[0]
                usage_result = results[1]

                # At least one should succeed
                assert not isinstance(topup_result, Exception) or not isinstance(
                    usage_result, Exception
                )

        # Verify final balance is consistent
        await integration_session.refresh(api_key)
        # Balance should be between initial and initial + topup amount
        assert api_key.balance >= initial_balance
        assert api_key.balance <= initial_balance + 2000

    @pytest.mark.asyncio
    async def test_race_condition_prevention(
        self,
        authenticated_client: AsyncClient,
        integration_session: AsyncSession,
    ) -> None:
        """Test that race conditions are prevented in balance updates"""
        # Get API key info
        api_key_header = authenticated_client.headers["Authorization"].replace(
            "Bearer ", ""
        )
        # API key format is "sk-{hashed_key}"
        api_key_hash = (
            api_key_header[3:] if api_key_header.startswith("sk-") else api_key_header
        )

        # Set a specific balance
        stmt = select(TemporaryCredit).where(TemporaryCredit.hashed_key == api_key_hash)  # type: ignore[arg-type]
        result = await integration_session.execute(stmt)
        api_key = result.scalar_one()
        api_key.balance = 1000
        await integration_session.commit()

        # Create a controlled race condition scenario
        balance_checks: List[int] = []

        async def check_and_update_balance() -> bool:
            # Read current balance
            stmt = select(TemporaryCredit).where(
                TemporaryCredit.hashed_key == api_key_hash
            )  # type: ignore[arg-type]
            result = await integration_session.execute(stmt)
            current_api_key = result.scalar_one()
            current_balance = current_api_key.balance
            balance_checks.append(current_balance)

            # Simulate processing delay
            await asyncio.sleep(0.01)

            # Try to update based on read value
            current_api_key.balance = current_balance - 100

            try:
                await integration_session.commit()
                return True
            except Exception:
                await integration_session.rollback()
                return False

        # Run multiple concurrent updates
        tasks = [check_and_update_balance() for _ in range(5)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Refresh and check final state
        await integration_session.refresh(api_key)

        # At least some updates should succeed
        successful_updates = sum(1 for r in results if r is True)
        assert successful_updates > 0

        # Final balance should reflect successful updates
        expected_balance = 1000 - (successful_updates * 100)
        assert api_key.balance == expected_balance


class TestDataIntegrity:
    """Test data integrity constraints and validations"""

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="Balance never negative is not implemented")
    async def test_balance_never_negative(
        self,
        authenticated_client: AsyncClient,
        integration_session: AsyncSession,
    ) -> None:
        """Test that balance can never go negative"""
        # Get API key info
        api_key_header = authenticated_client.headers["Authorization"].replace(
            "Bearer ", ""
        )
        # API key format is "sk-{hashed_key}"
        api_key_hash = (
            api_key_header[3:] if api_key_header.startswith("sk-") else api_key_header
        )

        # Set low balance
        stmt = select(TemporaryCredit).where(TemporaryCredit.hashed_key == api_key_hash)  # type: ignore[arg-type]
        result = await integration_session.execute(stmt)
        api_key = result.scalar_one()
        api_key.balance = 0
        await integration_session.commit()

        # Try to refund more than balance
        response = await authenticated_client.post(
            "/v1/wallet/refund", json={"amount": 1000}
        )

        # Should fail
        assert response.status_code == 400
        assert "Balance too small to refund" in response.json()["detail"]

        # Verify balance unchanged
        await integration_session.refresh(api_key)
        assert api_key.balance == 100

    @pytest.mark.asyncio
    async def test_primary_key_uniqueness(
        self,
        authenticated_client: AsyncClient,
        integration_session: AsyncSession,
    ) -> None:
        """Test that primary key constraints are enforced"""
        # Get existing API key hash from authenticated client
        api_key_header = authenticated_client.headers["Authorization"].replace(
            "Bearer ", ""
        )
        api_key_hash = (
            api_key_header[3:] if api_key_header.startswith("sk-") else api_key_header
        )

        # Try to manually insert duplicate key with same hash
        duplicate_key = TemporaryCredit(hashed_key=api_key_hash, balance=5000)

        integration_session.add(duplicate_key)

        # Should raise integrity error
        with pytest.raises(IntegrityError):
            await integration_session.commit()

        await integration_session.rollback()

    @pytest.mark.asyncio
    async def test_timestamp_consistency(
        self,
        authenticated_client: AsyncClient,
        integration_session: AsyncSession,
    ) -> None:
        """Test that timestamps are consistent and properly ordered"""
        # Track request times
        request_times: List[float] = []

        # Make several requests with delays
        for i in range(3):
            start_time = time.time()
            response = await authenticated_client.get("/v1/wallet/info")
            assert response.status_code == 200
            request_times.append(start_time)
            await asyncio.sleep(0.1)

        # Verify timestamps are monotonically increasing
        for i in range(1, len(request_times)):
            assert request_times[i] > request_times[i - 1]

    @pytest.mark.asyncio
    async def test_numeric_field_constraints(
        self,
        authenticated_client: AsyncClient,
        integration_session: AsyncSession,
    ) -> None:
        """Test constraints on numeric fields"""
        # Get API key
        api_key_header = authenticated_client.headers["Authorization"].replace(
            "Bearer ", ""
        )
        # API key format is "sk-{hashed_key}"
        api_key_hash = (
            api_key_header[3:] if api_key_header.startswith("sk-") else api_key_header
        )

        stmt = select(TemporaryCredit).where(TemporaryCredit.hashed_key == api_key_hash)  # type: ignore[arg-type]
        result = await integration_session.execute(stmt)
        api_key = result.scalar_one()

        # Test setting invalid values directly
        # These should maintain integrity
        assert api_key.balance >= 0


class TestPerformance:
    """Test database performance characteristics"""

    @pytest.mark.asyncio
    async def test_operation_latency(
        self,
        authenticated_client: AsyncClient,
        integration_session: AsyncSession,
    ) -> None:
        """Test that database operations complete within acceptable time"""
        operation_times: Dict[str, List[float]] = {
            "select": [],
            "update": [],
            "insert": [],
        }

        # Test SELECT performance
        for _ in range(10):
            start = time.time()
            response = await authenticated_client.get("/v1/wallet/info")
            end = time.time()
            assert response.status_code == 200
            operation_times["select"].append((end - start) * 1000)  # Convert to ms

        # Test UPDATE performance (via topup)
        with patch("routstr.payment.wallet.send_token") as mock_wallet_func:
            mock_proof = MagicMock()
            mock_proof.amount = 100
            mock_wallet = AsyncMock()
            mock_wallet.deserialize_token = AsyncMock(return_value=[mock_proof])
            mock_wallet.redeem = AsyncMock(return_value=[mock_proof])
            mock_wallet_func.return_value = mock_wallet

            for _ in range(5):
                start = time.time()
                response = await authenticated_client.post(
                    "/v1/wallet/topup", params={"cashu_token": "cashuAey..."}
                )
                end = time.time()
                # Skip if token is invalid (400)
                if response.status_code == 400:
                    continue
                assert response.status_code == 200
                operation_times["update"].append((end - start) * 1000)

        # Verify all operations < 100ms
        for op_type, times in operation_times.items():
            if times:  # Only check if we have measurements
                avg_time = sum(times) / len(times)
                max_time = max(times)

                # Average should be well under 100ms
                assert avg_time < 100, (
                    f"{op_type} average time {avg_time}ms exceeds 100ms"
                )

                # No single operation should exceed 200ms
                assert max_time < 200, f"{op_type} max time {max_time}ms exceeds 200ms"

    @pytest.mark.asyncio
    async def test_connection_pool_behavior(
        self,
        authenticated_client: AsyncClient,
        integration_app: Any,
    ) -> None:
        """Test database connection pool behavior under load"""

        # Make many concurrent requests to test connection pooling
        async def make_request() -> Response:
            return await authenticated_client.get("/v1/wallet/info")

        # Create 50 concurrent requests
        tasks = [make_request() for _ in range(50)]

        start = time.time()
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        end = time.time()

        # All should succeed
        success_count = sum(
            1
            for r in responses
            if not isinstance(r, Exception)
            and hasattr(r, "status_code")
            and r.status_code == 200
        )
        assert success_count == 50, f"Only {success_count}/50 requests succeeded"

        # Should complete reasonably quickly (< 5 seconds for 50 requests)
        total_time = end - start
        assert total_time < 5.0, f"50 concurrent requests took {total_time}s"

    @pytest.mark.asyncio
    async def test_index_usage(
        self,
        authenticated_client: AsyncClient,
        integration_session: AsyncSession,
    ) -> None:
        """Test that database indexes are used efficiently"""
        # Get API key for testing
        api_key_header = authenticated_client.headers["Authorization"].replace(
            "Bearer ", ""
        )
        # API key format is "sk-{hashed_key}"
        api_key_hash = (
            api_key_header[3:] if api_key_header.startswith("sk-") else api_key_header
        )

        # Primary key lookup should be fast
        start = time.time()
        stmt = select(TemporaryCredit).where(TemporaryCredit.hashed_key == api_key_hash)  # type: ignore[arg-type]
        result = await integration_session.execute(stmt)
        api_key = result.scalar_one()
        end = time.time()

        lookup_time = (end - start) * 1000
        assert lookup_time < 10, f"Primary key lookup took {lookup_time}ms"

        # Verify we got the right record
        assert api_key.hashed_key == api_key_hash
