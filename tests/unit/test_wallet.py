import base64
import json
from unittest.mock import AsyncMock, Mock, patch

import pytest

from router.wallet import credit_balance, get_balance, recieve_token, send_token


@pytest.mark.asyncio
async def test_get_balance() -> None:
    mock_wallet = Mock()
    mock_wallet.available_balance = Mock(amount=50000)
    mock_wallet.load_proofs = AsyncMock()

    with patch("router.wallet.Wallet.with_db", return_value=mock_wallet):
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
    mock_wallet.redeem = AsyncMock()

    with patch("router.wallet.TRUSTED_MINTS", ["http://mint:3338"]):
        with patch("router.wallet.deserialize_token_from_string") as mock_deserialize:
            mock_token = Mock()
            mock_token.keysets = ["keyset1"]
            mock_token.mint = "http://mint:3338"
            mock_token.unit = "sat"
            mock_token.amount = 1000
            mock_token.proofs = [{"amount": 1000}]
            mock_deserialize.return_value = mock_token

            with patch("router.wallet.Wallet.with_db", return_value=mock_wallet):
                mock_wallet.load_mint = AsyncMock()

                amount, unit, mint = await recieve_token(token_str)
                assert amount == 1000
                assert unit == "sat"
                assert mint == "http://mint:3338"


@pytest.mark.asyncio
async def test_send_token() -> None:
    mock_wallet = Mock()

    with patch("router.wallet.Wallet.with_db", return_value=mock_wallet):
        with patch("router.wallet.send", return_value=(1000, "test_token")):
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
    mock_session = AsyncMock()

    with patch("router.wallet.PRIMARY_MINT_URL", "http://mint:3338"):
        with patch(
            "router.wallet.recieve_token",
            return_value=(1000, "sat", "http://mint:3338"),
        ):
            amount = await credit_balance(token_str, mock_key, mock_session)
            assert amount == 1000000  # converted to msat
            assert mock_key.balance == 6000000
            mock_session.add.assert_called_once_with(mock_key)
            mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_credit_balance_invalid_mint() -> None:
    mock_key = Mock()
    mock_session = AsyncMock()

    with patch(
        "router.wallet.recieve_token", return_value=(1000, "sat", "http://other:3338")
    ):
        with pytest.raises(ValueError, match="Mint URL is not supported"):
            await credit_balance("test_token", mock_key, mock_session)


@pytest.mark.asyncio
async def test_recieve_token_untrusted_mint() -> None:
    mock_wallet = Mock()

    with patch("router.wallet.deserialize_token_from_string") as mock_deserialize:
        mock_token = Mock()
        mock_token.keysets = ["keyset1"]
        mock_token.mint = "http://untrusted:3338"
        mock_token.unit = "sat"
        mock_token.amount = 1000
        mock_deserialize.return_value = mock_token

        with patch("router.wallet.Wallet.with_db", return_value=mock_wallet):
            mock_wallet.load_mint = AsyncMock()
            with patch(
                "router.wallet.swap_to_primary_mint",
                return_value=(900, "sat", "http://mint:3338"),
            ):
                amount, unit, mint = await recieve_token("test_token")
                assert amount == 900
                assert unit == "sat"
                assert mint == "http://mint:3338"
