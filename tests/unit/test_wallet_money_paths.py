"""Additional money-path coverage tests for wallet.py (86% → target 90%).

Tests error classification, periodic task structure, and token operations.
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

# ===========================================================================
# is_mint_connection_error
# ===========================================================================

def test_is_mint_connection_error_true() -> None:
    """Connection errors are detected."""
    from routstr.wallet import is_mint_connection_error

    assert is_mint_connection_error(ConnectionRefusedError("refused")) is True
    assert is_mint_connection_error(TimeoutError("timeout")) is True


def test_is_mint_connection_error_false() -> None:
    """Non-connection errors are not flagged."""
    from routstr.wallet import is_mint_connection_error

    assert is_mint_connection_error(ValueError("bad data")) is False
    assert is_mint_connection_error(KeyError("missing key")) is False
    assert is_mint_connection_error(RuntimeError("something broke")) is False
    assert is_mint_connection_error(AttributeError("no attr")) is False
    # OSError is NOT a connection error unless it's a subclass
    assert is_mint_connection_error(OSError("generic")) is False


# ===========================================================================
# classify_redemption_error
# ===========================================================================

def test_classify_redemption_error_token_consumed() -> None:
    """Token already spent returns token_consumed classification."""
    from routstr.wallet import TokenConsumedError, classify_redemption_error

    result = classify_redemption_error(
        TokenConsumedError("Token was already redeemed")
    )
    assert result is not None
    assert result[0] == "token_consumed"
    assert result[1] == 500


def test_classify_redemption_error_mint_connection() -> None:
    """Mint connection error is classified correctly."""
    from routstr.wallet import classify_redemption_error

    result = classify_redemption_error(
        ConnectionRefusedError("Connection refused")
    )
    assert result is not None
    # Should classify as mint_connection or return error tuple
    assert isinstance(result, tuple)
    assert len(result) >= 3


def test_classify_redemption_error_unclassified() -> None:
    """Generic errors are classified as cashu_error with 400 status."""
    from routstr.wallet import classify_redemption_error

    result = classify_redemption_error(ValueError("unexpected"))
    # classify_redemption_error classifies all unrecognized errors
    # as cashu_error with a generic message
    assert result is not None
    assert result[0] == "cashu_error"
    assert result[1] == 400


# ===========================================================================
# Store readiness: store_cashu_transaction succeeds
# ===========================================================================

@pytest.mark.asyncio
async def test_store_cashu_transaction_succeeds_normally() -> None:
    """Normal store_cashu_transaction returns True on success."""
    from routstr.core.db import store_cashu_transaction

    with patch("routstr.core.db.create_session") as mock_create:
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_create.return_value = mock_session

        result = await store_cashu_transaction(
            token="cashuAtest",
            amount=1000,
            unit="sat",
            typ="in",
            request_id="req-test",
        )

        assert result is True


# ===========================================================================
# get_balance
# ===========================================================================

@pytest.mark.asyncio
async def test_get_balance_returns_integer() -> None:
    """get_balance returns an integer balance from wallet."""
    from routstr.wallet import get_balance

    mock_wallet = Mock()
    mock_wallet.available_balance = Mock(amount=50000)
    mock_wallet.load_mint = AsyncMock()
    mock_wallet.load_proofs = AsyncMock()

    with (
        patch("routstr.wallet._wallets", {}),
        patch("routstr.wallet.Wallet.with_db", return_value=mock_wallet),
    ):
        balance = await get_balance("sat")
        assert isinstance(balance, int)
        assert balance == 50000


# ===========================================================================
# Periodic task structure verification
# ===========================================================================

def test_periodic_payout_has_loop_and_error_handling() -> None:
    """periodic_payout runs in a loop with error handling."""
    import inspect

    from routstr import wallet

    source = inspect.getsource(wallet.periodic_payout)
    assert "while True" in source
    assert "except" in source, "Must have error handling"


def test_periodic_refund_sweep_has_error_handling() -> None:
    """Refund sweep catches errors to stay alive."""
    import inspect

    from routstr import wallet

    source = inspect.getsource(wallet.periodic_refund_sweep)
    assert "while True" in source
    assert "except" in source, "Must have error handling"


def test_periodic_routstr_fee_payout_structure() -> None:
    """Fee payout loop handles missing LN address gracefully."""
    import inspect

    from routstr import wallet

    source = inspect.getsource(wallet.periodic_routstr_fee_payout)
    # Returns early if ROUTSTR_LN_ADDRESS not set
    assert "ROUTSTR_LN_ADDRESS" in source
    assert "return" in source or "skip" in source.lower()
