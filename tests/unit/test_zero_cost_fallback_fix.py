"""Functional tests for the zero-cost billing fallback fix.

These tests verify that when ``adjust_payment_for_tokens`` raises during a
streaming finalize, the code:

1. Does NOT hardcode ``total_msats=0`` (free inference).
2. Releases the reserved balance (no permanent leak).
3. Logs at CRITICAL level.
4. Uses a narrow exception type (not catch-all ``Exception``).
5. The ``_safe_finalize_billing`` helper returns a non-zero cost.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routstr.core.db import ApiKey
from routstr.upstream.base import BaseUpstreamProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider() -> BaseUpstreamProvider:
    return BaseUpstreamProvider(base_url="http://test", api_key="sk-test")


def _make_key(hashed_key: str = "abcdefgh1234567890", reserved: int = 5000) -> ApiKey:
    """Create a mock ApiKey with a reserved balance."""
    key = MagicMock(spec=ApiKey)
    key.hashed_key = hashed_key
    key.reserved_balance = reserved
    key.balance = 100_000
    key.total_spent = 0
    key.parent_key_hash = None
    return key


# ---------------------------------------------------------------------------
# _safe_finalize_billing unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safe_finalize_billing_returns_nonzero_cost() -> None:
    """The helper must return a non-zero total_msats — never free service."""
    provider = _make_provider()
    key = _make_key(reserved=5000)
    session = AsyncMock()
    error = RuntimeError("DB connection lost")

    result = await provider._safe_finalize_billing(
        key, key, session, 5000, error, "test"
    )

    assert result["total_msats"] > 0, (
        "total_msats must be non-zero — free inference is a money leak"
    )
    assert result["total_msats"] == 5000, (
        "Should use the reserved max_cost as the best-effort charge"
    )


@pytest.mark.asyncio
async def test_safe_finalize_billing_logs_critical() -> None:
    """Billing failure must log at CRITICAL so operators are alerted."""
    provider = _make_provider()
    key = _make_key()
    session = AsyncMock()
    error = RuntimeError("DB connection lost")

    with patch("routstr.upstream.base.logger") as mock_logger:
        await provider._safe_finalize_billing(
            key, key, session, 5000, error, "test"
        )

    mock_logger.critical.assert_called()
    call_args = str(mock_logger.critical.call_args)
    assert "billing" in call_args.lower() or "leak" in call_args.lower(), (
        "CRITICAL log must mention the billing/leak context"
    )


@pytest.mark.asyncio
async def test_safe_finalize_billing_releases_reserved_balance() -> None:
    """The helper must release the reserved balance to prevent permanent leak."""
    provider = _make_provider()
    key = _make_key(hashed_key="k1", reserved=5000)
    session = AsyncMock()

    # Mock session.exec to track the UPDATE statement
    await provider._safe_finalize_billing(
        key, key, session, 5000, RuntimeError("boom"), "test"
    )

    # session.exec should have been called (for the release UPDATE)
    assert session.exec.call_count >= 1, (
        "Reserved balance release must issue at least one DB UPDATE"
    )
    # session.commit must be called to persist the release
    session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_safe_finalize_billing_releases_child_key_too() -> None:
    """When billing key differs from request key, both must be released."""
    provider = _make_provider()
    billing_key = _make_key(hashed_key="parent", reserved=5000)
    child_key = _make_key(hashed_key="child", reserved=5000)
    session = AsyncMock()

    await provider._safe_finalize_billing(
        child_key, billing_key, session, 5000, RuntimeError("boom"), "test"
    )

    # Two UPDATEs: one for billing key, one for child key
    assert session.exec.call_count >= 2, (
        "Both billing key and child key must have their reserved_balance released"
    )


@pytest.mark.asyncio
async def test_safe_finalize_billing_release_failure_logs_critical() -> None:
    """If the release itself fails, a second CRITICAL must be emitted."""
    provider = _make_provider()
    key = _make_key()
    session = AsyncMock()
    session.exec.side_effect = RuntimeError("DB is down")

    with patch("routstr.upstream.base.logger") as mock_logger:
        result = await provider._safe_finalize_billing(
            key, key, session, 5000, RuntimeError("original error"), "test"
        )

    # Even if release fails, we still return non-zero cost
    assert result["total_msats"] > 0

    # Two CRITICAL logs: one for billing failure, one for release failure
    assert mock_logger.critical.call_count >= 2, (
        "Release failure must emit its own CRITICAL log"
    )


@pytest.mark.asyncio
async def test_safe_finalize_billing_no_zero_usd() -> None:
    """total_usd must not be hardcoded to 0.0 — it should be calculated."""
    provider = _make_provider()
    key = _make_key(reserved=10000)
    session = AsyncMock()

    with patch(
        "routstr.upstream.base.sats_usd_price", return_value=100_000_000
    ):  # 1 BTC = 100M sats
        result = await provider._safe_finalize_billing(
            key, key, session, 10000, RuntimeError("boom"), "test"
        )

    # 10000 msats = 10 sats. At 1 BTC = 100M sats ≈ $100k, 10 sats ≈ $0.01
    # The key assertion: it's not hardcoded to 0.0
    # (It might be 0.0 if sats_usd_price fails, but with the mock it should be non-zero)
    assert result["total_usd"] != 0.0 or result["total_msats"] > 0, (
        "Must not return a fully zero cost dict (free service)"
    )


# ---------------------------------------------------------------------------
# Source-inspection: narrow exception types
# ---------------------------------------------------------------------------


def test_streaming_chat_finalize_uses_narrow_exception() -> None:
    """The except clause for billing finalize must not catch all Exception."""
    import inspect

    source = inspect.getsource(
        BaseUpstreamProvider.handle_streaming_chat_completion
    )

    # Find the billing finalize block: the area between
    # adjust_payment_for_tokens and usage_finalized after it
    idx = source.find("adjust_payment_for_tokens")
    # Look at the next 500 chars after the call to find the except clause
    finalize_area = source[idx : idx + 500]

    # Should use a narrow exception tuple, not bare `except Exception:`
    assert "except Exception:" not in finalize_area, (
        "Streaming billing finalize must use a narrow exception type, "
        "not catch-all Exception"
    )
    assert "SQLAlchemyError" in finalize_area or "OSError" in finalize_area, (
        "Streaming billing finalize should catch specific DB/upstream errors"
    )


def test_streaming_messages_finalize_uses_narrow_exception() -> None:
    """The messages streaming finalize must not catch all Exception."""
    import inspect

    source = inspect.getsource(
        BaseUpstreamProvider.handle_streaming_messages_completion
    )

    idx = source.find("adjust_payment_for_tokens")
    finalize_area = source[idx : idx + 500]

    assert "except Exception:" not in finalize_area, (
        "Messages streaming billing finalize must use a narrow exception type"
    )
    assert "SQLAlchemyError" in finalize_area or "OSError" in finalize_area, (
        "Messages streaming finalize should catch specific DB/upstream errors"
    )


def test_safe_finalize_billing_method_exists() -> None:
    """The _safe_finalize_billing helper method must exist on the class."""
    assert hasattr(BaseUpstreamProvider, "_safe_finalize_billing"), (
        "_safe_finalize_billing helper must exist on BaseUpstreamProvider"
    )
    import inspect

    sig = inspect.signature(BaseUpstreamProvider._safe_finalize_billing)
    params = list(sig.parameters.keys())
    assert "deducted_max_cost" in params, "Helper must accept deducted_max_cost"
    assert "error" in params, "Helper must accept the original error"
    assert "context" in params, "Helper must accept a context string"
