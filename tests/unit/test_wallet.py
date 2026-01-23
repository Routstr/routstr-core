import base64
import json
from unittest.mock import AsyncMock, MagicMock, Mock, patch

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
    # Ensure key has refund_mint_url set to match token mint to avoid migration
    mock_key.refund_mint_url = "http://mint:3338"
    mock_key.refund_currency = "sat"

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
            # We also need to patch deserialize_token_from_string since credit_balance calls it
            with patch(
                "routstr.wallet.deserialize_token_from_string"
            ) as mock_deserialize:
                mock_token = Mock()
                mock_token.mint = "http://mint:3338"
                mock_deserialize.return_value = mock_token

                amount = await credit_balance(token_str, mock_key, mock_session)
                assert amount == 1000000  # converted to msat
                assert mock_key.balance == 6000000  # Should be updated after refresh
                # Verify atomic operations were used
                assert mock_session.exec.called  # Atomic UPDATE statement
                assert mock_session.commit.called
                assert mock_session.refresh.called


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


# --- New Tests for Multi-Mint Logic ---

PRIMARY_MINT = "https://primary-mint.com"
MINT_A = "https://mint-a.com"
MINT_B = "https://mint-b.com"


@pytest.fixture
def mock_multi_mint_context():
    from routstr.core.settings import settings

    with (
        patch.object(settings, "primary_mint", PRIMARY_MINT),
        patch.object(settings, "cashu_mints", [PRIMARY_MINT, MINT_A, MINT_B]),
        patch("routstr.wallet.recieve_token", new_callable=AsyncMock) as mock_recv,
        patch(
            "routstr.wallet.migrate_balance_to_primary", new_callable=AsyncMock
        ) as mock_mig,
        patch("routstr.wallet.deserialize_token_from_string") as mock_deser,
    ):
        mock_recv.return_value = (1000, "sat", PRIMARY_MINT)
        yield {
            "recieve_token": mock_recv,
            "migrate": mock_mig,
            "deserialize": mock_deser,
        }


@pytest.mark.asyncio
async def test_topup_different_mint_migrates_and_forces_swap(mock_multi_mint_context):
    """
    Case: User is on Mint A, tops up with Mint B token.
    Expectation:
    1. Migrate existing Mint A balance to Primary.
    2. Force swap incoming Mint B token to Primary.
    """
    ctx = mock_multi_mint_context

    # Setup key on Mint A
    key = Mock(spec=ApiKey)
    key.hashed_key = "hash"
    key.balance = 1000
    key.refund_mint_url = MINT_A
    key.refund_currency = "sat"

    session = AsyncMock()

    # Setup token from Mint B
    token_obj = MagicMock()
    token_obj.mint = MINT_B
    ctx["deserialize"].return_value = token_obj

    await credit_balance("cashuA_token_mint_b", key, session)

    # Verification
    ctx["migrate"].assert_called_once_with(key, session)
    ctx["recieve_token"].assert_called_once_with(
        "cashuA_token_mint_b", force_primary=True
    )


@pytest.mark.asyncio
async def test_topup_same_mint_as_refund_mint(mock_multi_mint_context):
    """
    Case: User is on Mint A, tops up with Mint A token.
    Expectation: No migration, no forced swap.
    """
    ctx = mock_multi_mint_context

    # Setup key
    key = Mock(spec=ApiKey)
    key.hashed_key = "hash"
    key.balance = 1000
    key.refund_mint_url = MINT_A
    key.refund_currency = "sat"

    session = AsyncMock()

    # Setup token to match Mint A
    token_obj = MagicMock()
    token_obj.mint = MINT_A
    ctx["deserialize"].return_value = token_obj

    await credit_balance("cashuA_token_mint_a", key, session)

    # Verification
    ctx["migrate"].assert_not_called()
    ctx["recieve_token"].assert_called_once_with(
        "cashuA_token_mint_a", force_primary=False
    )


@pytest.mark.asyncio
async def test_topup_primary_when_on_primary(mock_multi_mint_context):
    """
    Case: User is on Primary (or None), tops up with Primary token.
    Expectation: No migration, no forced swap.
    """
    ctx = mock_multi_mint_context

    # Setup key on Primary
    key = Mock(spec=ApiKey)
    key.hashed_key = "hash"
    key.balance = 1000
    key.refund_mint_url = PRIMARY_MINT
    key.refund_currency = "sat"

    session = AsyncMock()

    # Setup token from Primary
    token_obj = MagicMock()
    token_obj.mint = PRIMARY_MINT
    ctx["deserialize"].return_value = token_obj

    await credit_balance("cashuA_token_primary", key, session)

    # Verification
    ctx["migrate"].assert_not_called()
    ctx["recieve_token"].assert_called_once_with(
        "cashuA_token_primary", force_primary=False
    )


@pytest.mark.asyncio
async def test_topup_mint_a_when_on_primary(mock_multi_mint_context):
    """
    Case: User is on Primary, tops up with Mint A token.
    Expectation: No migration (already on primary), but force swap incoming token.
    """
    ctx = mock_multi_mint_context

    # Setup key on Primary
    key = Mock(spec=ApiKey)
    key.hashed_key = "hash"
    key.balance = 1000
    key.refund_mint_url = PRIMARY_MINT
    key.refund_currency = "sat"

    session = AsyncMock()

    # Setup token from Mint A
    token_obj = MagicMock()
    token_obj.mint = MINT_A
    ctx["deserialize"].return_value = token_obj

    await credit_balance("cashuA_token_mint_a", key, session)

    # Verification
    ctx["migrate"].assert_not_called()
    ctx["recieve_token"].assert_called_once_with(
        "cashuA_token_mint_a", force_primary=True
    )
