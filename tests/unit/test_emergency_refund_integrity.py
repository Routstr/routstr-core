"""Tests exposing the emergency refund try/except/pass vulnerability.

The emergency refund paths in base.py (lines 3627-3668 for chat, 4591-4632 for
responses) mint a refund token via send_token(), then store it via
store_cashu_transaction() wrapped in try/except/pass.  If the DB write fails,
the token is already minted but unrecoverable — funds are silently lost.

These tests document the current behaviour and will FAIL when the vulnerability
is fixed (they assert that the DB store is inside try/except/pass and that a
store failure is silently swallowed).
"""

import json
from unittest.mock import AsyncMock, Mock, patch

import pytest


# ---------------------------------------------------------------------------
# Reproduce vulnerability: store_cashu_transaction can fail silently
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_store_cashu_transaction_catches_all_exceptions() -> None:
    """store_cashu_transaction returns False instead of raising on DB failure.

    This is the root cause of the emergency-refund fund-loss vulnerability:
    callers that use try/except/pass never know the store failed.
    """
    from routstr.core.db import store_cashu_transaction

    with patch("routstr.core.db.create_session") as mock_create:
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock(side_effect=OSError("disk full"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_create.return_value = mock_session

        # Should NOT raise — catches all exceptions and returns False
        result = await store_cashu_transaction(
            token="cashuAtest_refund_token",
            amount=1000,
            unit="sat",
            mint_url="http://mint:3338",
            typ="out",
            request_id="req-123",
        )

        assert result is False, (
            "BUG: store_cashu_transaction returns False on failure. "
            "Callers using try/except/pass never detect the failure."
        )


@pytest.mark.asyncio
async def test_store_cashu_transaction_silent_returns_bool_only() -> None:
    """store_cashu_transaction never raises — it only returns True/False.

    Every caller that does `except Exception: pass` around this call will
    silently lose the transaction record if the store fails.
    """
    from routstr.core.db import store_cashu_transaction

    with patch("routstr.core.db.create_session") as mock_create:
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock(side_effect=RuntimeError("any error"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_create.return_value = mock_session

        result = await store_cashu_transaction(
            token="cashuAtest_token",
            amount=500,
            unit="msat",
            typ="in",
            request_id="req-456",
        )

        assert result is False, (
            "BUG: store_cashu_transaction swallows RuntimeError. "
            "Funds minted before this call are now unrecoverable."
        )


# ---------------------------------------------------------------------------
# Reproduce vulnerability: emergency refund paths exist and are duplicated
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_emergency_refund_exception_handler_exists_chat() -> None:
    """Verify the chat emergency refund handler exists with try/except/pass.

    base.py lines 3627-3668 handle JSONDecodeError in chat non-streaming
    responses by issuing an emergency refund via send_token(). The subsequent
    store_cashu_transaction is wrapped in try/except/pass at lines 3643-3653.
    """
    from routstr.upstream.base import BaseUpstreamProvider

    # Verify the method that contains this handler exists
    assert hasattr(BaseUpstreamProvider, "handle_x_cashu_non_streaming_response"), (
        "handle_x_cashu_non_streaming_response is the method containing "
        "the chat emergency refund path (lines 3627-3668)"
    )

    # Read the source to verify the try/except/pass pattern
    import inspect

    source = inspect.getsource(
        BaseUpstreamProvider.handle_x_cashu_non_streaming_response
    )
    assert "emergency_refund = amount" in source, (
        "BUG: Emergency refund path exists — mints token via send_token() "
        "then stores in try/except/pass. DB failure = silent fund loss."
    )
    # Verify the try/except/pass around store_cashu_transaction
    assert "except Exception:" in source, (
        "BUG CONFIRMED: Emergency refund uses try/except/pass — "
        "any DB store failure is silently swallowed."
    )
    assert "pass" in source.split("except Exception:")[1][:50], (
        "BUG CONFIRMED: The except block contains 'pass' — no recovery, "
        "no CRITICAL log, no token retention."
    )


@pytest.mark.asyncio
async def test_emergency_refund_handler_duplicated() -> None:
    """Verify the emergency refund pattern is duplicated (chat + responses).

    base.py has TWO nearly identical emergency refund blocks:
    - Chat API: lines 3627-3668
    - Responses API: lines 4591-4632

    Both use the same try/except/pass pattern. This duplication means
    any fix must be applied in TWO places.
    """
    from routstr.upstream.base import BaseUpstreamProvider

    import inspect

    chat_source = inspect.getsource(
        BaseUpstreamProvider.handle_x_cashu_non_streaming_response
    )
    responses_source = inspect.getsource(
        BaseUpstreamProvider.handle_x_cashu_non_streaming_responses_response
    )

    chat_has_emergency = "emergency_refund = amount" in chat_source
    responses_has_emergency = "emergency_refund = amount" in responses_source

    assert chat_has_emergency, "Chat path has emergency refund"
    assert responses_has_emergency, "Responses path has emergency refund"

    assert chat_has_emergency and responses_has_emergency, (
        "BUG CONFIRMED: Emergency refund is duplicated across Chat and "
        "Responses API paths. Both use try/except/pass. A fix must be "
        "applied in TWO places."
    )


# ---------------------------------------------------------------------------
# Document: emergency refund mints BEFORE storing (the ordering is the bug)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_token_mints_before_database() -> None:
    """send_token() mints proofs at the mint BEFORE any caller stores to DB.

    This is the core assumption behind the vulnerability: if the contract
    is "mint first, store second", then any store failure = lost token.
    """
    from routstr.wallet import send_token, send

    # send() serializes proofs and reserves them — no DB call
    with patch("routstr.wallet.get_wallet") as mock_get_wallet:
        mock_wallet = Mock()
        mock_wallet.keysets = {"ks1": Mock(mint_url="http://mint:3338", unit=Mock(name="sat"))}
        mock_wallet.proofs = []
        mock_wallet.select_to_send = AsyncMock(return_value=([], []))
        mock_wallet.serialize_proofs = AsyncMock(return_value="cashuAtest_token")
        mock_wallet.set_reserved_for_send = AsyncMock()
        mock_get_wallet.return_value = mock_wallet

        with patch("routstr.wallet.get_proofs_per_mint_and_unit") as mock_get_proofs:
            mock_get_proofs.return_value = []

            # send_token calls send(), which serializes first
            token = await send_token(1000, "sat")

            assert token == "cashuAtest_token", (
                "send_token returns a minted token. No DB store happens here. "
                "The caller is responsible for persisting — if they use "
                "try/except/pass, the token is lost."
            )
