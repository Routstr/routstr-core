import base64
import json
from unittest.mock import AsyncMock, Mock, patch

import pytest

from routstr.core.db import ApiKey
from routstr.wallet import credit_balance, get_balance, recieve_token, send_token


@pytest.mark.asyncio
async def test_get_balance() -> None:
    mock_wallet = Mock()
    mock_wallet.available_balance = Mock(amount=50000)
    mock_wallet.load_mint = AsyncMock()
    mock_wallet.load_proofs = AsyncMock()

    with patch("routstr.wallet.Wallet.with_db", return_value=mock_wallet):
        balance = await get_balance("sat")
        assert balance == 50000


@pytest.mark.asyncio
async def test_recieve_token_valid() -> None:
    token_data = {
        "token": [
            {
                "mint": "http://mint:3338",
                "proofs": [
                    {"amount": 1000, "id": "test", "secret": "secret", "C": "curve"}
                ],
            }
        ],
        "unit": "sat",
    }
    token_json = json.dumps(token_data)
    token_b64 = base64.urlsafe_b64encode(token_json.encode()).decode()
    token_str = f"cashuA{token_b64}"

    mock_wallet = Mock()
    mock_wallet.split = AsyncMock()

    from routstr.core.settings import settings

    with patch.object(settings, "cashu_mints", ["http://mint:3338"]):
        with patch("routstr.wallet.deserialize_token_from_string") as mock_deserialize:
            mock_token = Mock()
            mock_token.keysets = ["keyset1"]
            mock_token.mint = "http://mint:3338"
            mock_token.unit = "sat"
            mock_token.amount = 1000
            mock_token.proofs = [{"amount": 1000}]
            mock_deserialize.return_value = mock_token

            mock_wallet.load_mint = AsyncMock()
            mock_wallet.load_proofs = AsyncMock()
            with patch("routstr.wallet.Wallet.with_db", return_value=mock_wallet):
                amount, unit, mint = await recieve_token(token_str)
                assert amount == 1000
                assert unit == "sat"
                assert mint == "http://mint:3338"


@pytest.mark.asyncio
async def test_send_token() -> None:
    mock_wallet = Mock()

    with patch("routstr.wallet.Wallet.with_db", return_value=mock_wallet):
        with patch("routstr.wallet.send", return_value=(1000, "test_token")):
            token = await send_token(1000, "sat", "http://mint:3338")
            assert token == "test_token"


@pytest.mark.asyncio
async def test_credit_balance() -> None:
    token_data = {
        "token": [{"mint": "http://mint:3338", "proofs": [{"amount": 1000}]}],
        "unit": "sat",
    }
    token_json = json.dumps(token_data)
    token_b64 = base64.urlsafe_b64encode(token_json.encode()).decode()
    token_str = f"cashuA{token_b64}"

    mock_key = Mock()
    mock_key.balance = 5000000
    mock_key.hashed_key = "test_hash"
    mock_session = AsyncMock()

    # Mock session.refresh to update the balance (simulates DB reload)
    async def mock_refresh(key: ApiKey) -> None:
        key.balance = 6000000

    mock_session.refresh.side_effect = mock_refresh

    from routstr.core.settings import settings

    with patch.object(settings, "cashu_mints", ["http://mint:3338"]):
        with patch(
            "routstr.wallet.recieve_token",
            return_value=(1000, "sat", "http://mint:3338"),
        ):
            amount = await credit_balance(token_str, mock_key, mock_session)
            assert amount == 1000000  # converted to msat
            assert mock_key.balance == 6000000  # Should be updated after refresh
            # Verify atomic operations were used
            assert mock_session.exec.called  # Atomic UPDATE statement
            assert mock_session.commit.called
            assert mock_session.refresh.called


@pytest.mark.asyncio
async def test_swap_to_primary_mint_insufficient_for_fees() -> None:
    """Token amount is less than melt_quote.amount + melt_quote.fee_reserve."""
    from routstr.wallet import swap_to_primary_mint

    mock_token = Mock()
    mock_token.mint = "http://foreign:3338"
    mock_token.unit = "sat"
    mock_token.amount = 404
    mock_token.keysets = ["keyset1"]
    mock_token.proofs = [{"amount": 404}]

    mock_token_wallet = Mock()
    mock_token_wallet.load_mint = AsyncMock()
    mock_token_wallet.load_proofs = AsyncMock()

    mock_primary_wallet = Mock()
    mock_primary_wallet.load_mint = AsyncMock()
    mock_primary_wallet.load_proofs = AsyncMock()

    mock_mint_quote = Mock()
    mock_mint_quote.quote = "mint_quote_123"
    mock_mint_quote.request = "lnbc1..."
    mock_primary_wallet.request_mint = AsyncMock(return_value=mock_mint_quote)

    mock_melt_quote = Mock()
    mock_melt_quote.quote = "melt_quote_123"
    mock_melt_quote.amount = 400
    mock_melt_quote.fee_reserve = 12  # total needed: 412 > 404
    mock_token_wallet.melt_quote = AsyncMock(return_value=mock_melt_quote)

    from routstr.core.settings import settings

    with patch.object(settings, "primary_mint", "http://primary:3338"):
        with patch.object(settings, "primary_mint_unit", "sat"):
            with patch("routstr.wallet.get_wallet", return_value=mock_primary_wallet):
                with pytest.raises(ValueError, match="insufficient to cover melt fees"):
                    await swap_to_primary_mint(mock_token, mock_token_wallet)

    # melt should never have been called
    mock_token_wallet.melt.assert_not_called()


@pytest.mark.asyncio
async def test_swap_to_primary_mint_melt_error_wrapped() -> None:
    """Melt failure from cashu lib is wrapped as ValueError."""
    from routstr.wallet import swap_to_primary_mint

    mock_token = Mock()
    mock_token.mint = "http://foreign:3338"
    mock_token.unit = "sat"
    mock_token.amount = 5000
    mock_token.keysets = ["keyset1"]
    mock_token.proofs = [{"amount": 5000}]

    mock_token_wallet = Mock()
    mock_token_wallet.load_mint = AsyncMock()
    mock_token_wallet.load_proofs = AsyncMock()

    mock_primary_wallet = Mock()
    mock_primary_wallet.load_mint = AsyncMock()
    mock_primary_wallet.load_proofs = AsyncMock()

    mock_mint_quote = Mock()
    mock_mint_quote.quote = "mint_quote_456"
    mock_mint_quote.request = "lnbc1..."
    mock_primary_wallet.request_mint = AsyncMock(return_value=mock_mint_quote)

    mock_melt_quote = Mock()
    mock_melt_quote.quote = "melt_quote_456"
    mock_melt_quote.amount = 4940
    mock_melt_quote.fee_reserve = 50  # total 4990 < 5000, passes fee check
    mock_token_wallet.melt_quote = AsyncMock(return_value=mock_melt_quote)
    mock_token_wallet.melt = AsyncMock(
        side_effect=Exception("Provided: 5000, needed: 5100 (Code: 11000)")
    )

    from routstr.core.settings import settings

    with patch.object(settings, "primary_mint", "http://primary:3338"):
        with patch.object(settings, "primary_mint_unit", "sat"):
            with patch("routstr.wallet.get_wallet", return_value=mock_primary_wallet):
                with pytest.raises(ValueError, match="Failed to melt token"):
                    await swap_to_primary_mint(mock_token, mock_token_wallet)


@pytest.mark.asyncio
async def test_recieve_token_untrusted_mint() -> None:
    mock_wallet = Mock()

    with patch("routstr.wallet.deserialize_token_from_string") as mock_deserialize:
        mock_token = Mock()
        mock_token.keysets = ["keyset1"]
        mock_token.mint = "http://untrusted:3338"
        mock_token.unit = "sat"
        mock_token.amount = 1000
        mock_deserialize.return_value = mock_token

        mock_wallet.load_mint = AsyncMock()
        mock_wallet.load_proofs = AsyncMock()
        with patch("routstr.wallet.Wallet.with_db", return_value=mock_wallet):
            with patch(
                "routstr.wallet.swap_to_primary_mint",
                return_value=(900, "sat", "http://mint:3338"),
            ):
                amount, unit, mint = await recieve_token("test_token")
                assert amount == 900
                assert unit == "sat"
                assert mint == "http://mint:3338"


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_swap_to_primary_mint_already_on_primary() -> None:
    from routstr.core.settings import settings
    from routstr.wallet import swap_to_primary_mint

    mock_token = Mock()
    mock_token.mint = settings.primary_mint
    mock_token.amount = 1000
    mock_token.unit = "sat"
    mock_token.proofs = []

    mock_token_wallet = Mock()
    mock_token_wallet.split = AsyncMock(return_value=None)
    mock_token_wallet.request_mint = AsyncMock()
    mock_token_wallet.melt_quote = AsyncMock()

    with patch("routstr.wallet.get_wallet", AsyncMock(return_value=mock_token_wallet)):
        amount, unit, mint = await swap_to_primary_mint(mock_token, mock_token_wallet)

    assert amount == 1000
    assert unit == "sat"
    assert mint == settings.primary_mint
    mock_token_wallet.split.assert_called_once()
    mock_token_wallet.request_mint.assert_not_called()
    mock_token_wallet.melt_quote.assert_not_called()


async def test_swap_to_primary_mint_success() -> None:
    """Test successful swap with dynamic fee calculation."""
    from routstr.wallet import swap_to_primary_mint

    mock_token = Mock()
    mock_token.mint = "http://foreign:3338"
    mock_token.unit = "sat"
    mock_token.amount = 1000
    mock_token.keysets = ["keyset1"]
    mock_token.proofs = [{"amount": 1000}]

    mock_token_wallet = Mock()
    mock_token_wallet.load_mint = AsyncMock()
    mock_token_wallet.load_proofs = AsyncMock()

    mock_primary_wallet = Mock()
    mock_primary_wallet.load_mint = AsyncMock()
    mock_primary_wallet.load_proofs = AsyncMock()

    # Mocks for the estimation phase
    # 1. request_mint(dummy_amount=1000) -> invoice_dummy
    # 2. melt_quote(invoice_dummy) -> fee=10

    # Mocks for the execution phase
    # 3. request_mint(minted_amount=990) -> invoice_real
    # 4. melt_quote(invoice_real) -> amount=990, fee=10
    # 5. melt() -> success
    # 6. mint() -> success

    mock_mint_quote_dummy = Mock(quote="dummy_quote", request="lnbc_dummy")
    mock_mint_quote_real = Mock(quote="real_quote", request="lnbc_real")

    # side_effect for request_mint to return dummy then real
    mock_primary_wallet.request_mint = AsyncMock(
        side_effect=[mock_mint_quote_dummy, mock_mint_quote_real]
    )

    mock_melt_quote_dummy = Mock(amount=1000, fee_reserve=10)
    mock_melt_quote_real = Mock(amount=990, fee_reserve=10)

    # side_effect for melt_quote
    mock_token_wallet.melt_quote = AsyncMock(
        side_effect=[mock_melt_quote_dummy, mock_melt_quote_real]
    )

    mock_token_wallet.melt = AsyncMock(return_value="melted_proofs")
    mock_primary_wallet.mint = AsyncMock(return_value="minted_proofs")

    from routstr.core.settings import settings

    with patch.object(settings, "primary_mint", "http://primary:3338"):
        with patch.object(settings, "primary_mint_unit", "sat"):
            with patch("routstr.wallet.get_wallet", return_value=mock_primary_wallet):
                amount, unit, mint = await swap_to_primary_mint(
                    mock_token, mock_token_wallet
                )

                assert amount == 990  # 1000 - 10
                assert unit == "sat"
                assert mint == "http://primary:3338"

                # Verify call order/counts
                assert mock_primary_wallet.request_mint.call_count == 2
                # First call with full amount for estimation
                mock_primary_wallet.request_mint.assert_any_call(1000)
                # Second call with calculated amount
                mock_primary_wallet.request_mint.assert_any_call(990)

                assert mock_token_wallet.melt_quote.call_count == 2
                assert mock_token_wallet.melt.called
                assert mock_primary_wallet.mint.called
