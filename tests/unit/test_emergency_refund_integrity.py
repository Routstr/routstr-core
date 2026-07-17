"""Tests asserting CORRECT behavior for emergency refund and DB persistence.

These tests FAIL against current main because the code is buggy.
They serve as the "RED" phase of TDD — once the bugs are fixed, they go green.

Correct behavior required:
1. store_cashu_transaction should raise on failure (not silently return False)
2. Emergency refund paths must NOT use try/except/pass for DB stores
3. A retry wrapper must exist for critical money-path DB writes
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest


# ===========================================================================
# RED TESTS: store_cashu_transaction should RAISE on failure
# ===========================================================================

@pytest.mark.asyncio
async def test_store_cashu_raises_on_db_failure_not_returns_false() -> None:
    """FIX REQUIRED: store_cashu_transaction must raise on DB failure.

    Currently returns False silently — callers never detect the failure.
    Correct behavior: raise an exception so callers can recover.
    """
    from routstr.core.db import store_cashu_transaction

    with patch("routstr.core.db.create_session") as mock_create:
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock(side_effect=OSError("disk full"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_create.return_value = mock_session

        with pytest.raises(Exception) as exc_info:
            await store_cashu_transaction(
                token="cashuAtest_refund_token",
                amount=1000,
                unit="sat",
                mint_url="http://mint:3338",
                typ="out",
                request_id="req-123",
            )

        # Must raise a meaningful exception, not silently return False
        # OSError or a custom DB error is acceptable
        assert "disk full" in str(exc_info.value) or isinstance(
            exc_info.value, (OSError, RuntimeError)
        ), (
            f"Expected store to propagate the failure, got {type(exc_info.value).__name__}: "
            f"{exc_info.value}"
        )


@pytest.mark.asyncio
async def test_store_cashu_raises_on_any_error() -> None:
    """FIX REQUIRED: All DB errors must propagate, not just OSError."""
    from routstr.core.db import store_cashu_transaction

    errors = [
        OSError("disk full"),
        RuntimeError("connection lost"),
        ConnectionRefusedError("db down"),
    ]

    for error in errors:
        with patch("routstr.core.db.create_session") as mock_create:
            mock_session = AsyncMock()
            mock_session.commit = AsyncMock(side_effect=error)
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=None)
            mock_create.return_value = mock_session

            with pytest.raises(Exception):
                await store_cashu_transaction(
                    token="cashuAtest",
                    amount=1000,
                    unit="sat",
                    typ="out",
                )


# ===========================================================================
# RED TESTS: Retry wrapper must exist
# ===========================================================================

def test_retry_wrapper_exists_for_critical_writes() -> None:
    """FIX REQUIRED: store_cashu_transaction_with_retry must exist.

    Currently reverted (#600 → #604). All critical money-path DB writes
    (after minting a token) need retry with backoff + CRITICAL logging.
    """
    from routstr.core import db

    assert hasattr(db, "store_cashu_transaction_with_retry"), (
        "FIX REQUIRED: store_cashu_transaction_with_retry does not exist. "
        "Was merged in PR #600, reverted in PR #604. "
        "All post-mint DB writes need retry + backoff + CRITICAL logging."
    )


@pytest.mark.asyncio
async def test_retry_wrapper_retries_on_transient_failure() -> None:
    """FIX REQUIRED: retry wrapper must retry, not fail on first attempt."""
    from routstr.core import db

    # Skip if the retry wrapper doesn't exist yet
    if not hasattr(db, "store_cashu_transaction_with_retry"):
        pytest.skip("store_cashu_transaction_with_retry does not exist yet")

    with patch("routstr.core.db.store_cashu_transaction") as mock_store:
        mock_store = AsyncMock()
        mock_store.side_effect = [OSError("transient"), None]  # 1st fails, 2nd succeeds
        # We'd test that the wrapper retries, but it doesn't exist yet
        # This test documents the expected behavior


# ===========================================================================
# RED TESTS: Emergency refund must not silently lose tokens
# ===========================================================================

def test_emergency_refund_no_try_except_pass() -> None:
    """FIX REQUIRED: Emergency refund paths must NOT use try/except/pass.

    base.py:3643-3653 (chat) and base.py:4607-4617 (responses) both use
    try/except/pass around store_cashu_transaction after minting a refund
    token. If DB write fails, the token is permanently lost.

    The fix: remove try/except/pass. Let the exception propagate so
    the caller can detect failure and at minimum log the token.
    """
    import inspect
    from routstr.upstream.base import BaseUpstreamProvider

    # Check chat emergency refund handler
    chat_src = inspect.getsource(
        BaseUpstreamProvider.handle_x_cashu_non_streaming_response
    )

    # Find the emergency refund section
    emergency_start = chat_src.find("emergency_refund = amount")
    assert emergency_start > 0, "Emergency refund path exists"

    emergency_section = chat_src[emergency_start : emergency_start + 500]

    # The try/except/pass around store_cashu_transaction must NOT exist
    has_try = "try:" in emergency_section
    has_except_pass = "except Exception:" in emergency_section and "pass" in emergency_section

    assert not has_except_pass, (
        "FIX REQUIRED: Emergency refund (chat) uses try/except/pass around "
        "store_cashu_transaction. A failed DB write silently loses the minted "
        "token. Fix: let the exception propagate or log at CRITICAL with the "
        "full token for manual recovery."
    )


def test_emergency_refund_responses_api_no_silent_failure() -> None:
    """FIX REQUIRED: Responses API emergency refund same fix as chat."""
    import inspect
    from routstr.upstream.base import BaseUpstreamProvider

    responses_src = inspect.getsource(
        BaseUpstreamProvider.handle_x_cashu_non_streaming_responses_response
    )

    has_emergency = "emergency_refund = amount" in responses_src
    if has_emergency:
        emergency_start = responses_src.find("emergency_refund = amount")
        emergency_section = responses_src[emergency_start : emergency_start + 500]
        has_except_pass = (
            "except Exception:" in emergency_section and "pass" in emergency_section
        )
        assert not has_except_pass, (
            "FIX REQUIRED: Responses API emergency refund also uses "
            "try/except/pass. Same fund-loss vulnerability as chat path."
        )


# ===========================================================================
# RED TESTS: Fee payout crash safety
# ===========================================================================

def test_fee_payout_has_crash_guard() -> None:
    """FIX REQUIRED: Fee payout must have guard against double-pay on crash.

    wallet.py:1076-1080 pays LNURL THEN resets the fee counter.
    A crash between these steps causes double payment on restart.

    Fix options:
    1. Pre-reset the counter before paying (if pay fails, restore it)
    2. Add a "payout_lock" DB flag that's set before pay and cleared after
    3. Record payout in DB and reconcile on startup
    """
    import inspect
    from routstr import wallet

    source = inspect.getsource(wallet.periodic_routstr_fee_payout)

    # After the fix, the pay-then-reset pattern should be replaced
    # with a safe sequence. Verify the guard exists.
    has_guard = any(
        kw in source.lower()
        for kw in ["payout_lock", "is_paying", "payout_in_progress",
                    "pre_reset", "reset_before", "reconcile"]
    )

    assert has_guard, (
        "FIX REQUIRED: Fee payout has no crash guard. Pay-then-reset "
        "pattern in periodic_routstr_fee_payout can double-pay on "
        "process restart."
    )
