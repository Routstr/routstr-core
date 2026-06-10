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
async def test_credit_balance_rejects_zero_amount() -> None:
    """A zero/dust redemption must raise BEFORE any commit, so no orphan
    zero-balance key (balance 0, total_spent 0, total_requests 0) is persisted."""
    token_data = {
        "token": [{"mint": "http://mint:3338", "proofs": [{"amount": 0}]}],
        "unit": "sat",
    }
    token_json = json.dumps(token_data)
    token_b64 = base64.urlsafe_b64encode(token_json.encode()).decode()
    token_str = f"cashuA{token_b64}"

    mock_key = Mock()
    mock_key.balance = 0
    mock_key.hashed_key = "test_hash"
    mock_session = AsyncMock()

    from routstr.core.settings import settings

    with patch.object(settings, "cashu_mints", ["http://mint:3338"]):
        with patch(
            "routstr.wallet.recieve_token",
            return_value=(0, "sat", "http://mint:3338"),
        ):
            with pytest.raises(ValueError, match="must be positive"):
                await credit_balance(token_str, mock_key, mock_session)

    # Critically: no balance UPDATE and no commit happened, so the caller's
    # uncommitted key row rolls back instead of persisting as an orphan.
    assert not mock_session.exec.called
    assert not mock_session.commit.called


@pytest.mark.asyncio
async def test_swap_to_primary_mint_insufficient_for_fees() -> None:
    """Token amount is less than melt_quote.amount + melt_quote.fee_reserve.
    The quote mocks are static, so every retry observes the same shortfall —
    the swap must still give up and raise."""
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
    mock_token_wallet.get_fees_for_proofs = Mock(return_value=0)

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


# ---------------------------------------------------------------------------
# Swap fee estimation and reactive retry
#
# Spec: the estimation pass subtracts only observed fees (no safety buffer).
# swap_to_primary_mint then runs the mint-quote/melt-quote/melt cycle and, when
# the foreign mint demands more than estimated (at quote or at melt time),
# retries with the amount recomputed from the observed fee — at most 3 attempts.
# Melt failures unrelated to fees are not retried.
# ---------------------------------------------------------------------------


def _make_swap_mocks(
    token_amount: int,
    fee_reserves: list[int],
    input_fees: int = 0,
    mint_url: str = "http://foreign-mint:3338",
) -> tuple[Mock, Mock, Mock]:
    """Return (token, token_wallet, primary_wallet) mocks that act like a mint.

    Mint quotes pass the requested amount through their ``request`` field and
    melt quotes echo that amount back, so the mocks stay consistent for
    whatever amounts the implementation requests. ``fee_reserves`` supplies the
    fee_reserve of each successive melt quote (the first serves the estimation
    pass); requesting more quotes than provided fails the test.
    """
    mock_token = Mock()
    mock_token.mint = mint_url
    mock_token.unit = "sat"
    mock_token.amount = token_amount
    mock_token.keysets = ["keyset1"]
    mock_token.proofs = [Mock(amount=token_amount)]

    mock_token_wallet = Mock()
    mock_token_wallet.load_mint = AsyncMock()
    mock_token_wallet.load_proofs = AsyncMock()
    mock_token_wallet.get_fees_for_proofs = Mock(return_value=input_fees)

    mock_primary_wallet = Mock()
    mock_primary_wallet.load_mint = AsyncMock()
    mock_primary_wallet.load_proofs = AsyncMock()
    mock_primary_wallet.available_balance = Mock(amount=0)
    mock_primary_wallet.mint = AsyncMock(return_value=Mock())

    fees = iter(fee_reserves)

    def _next_fee() -> int:
        try:
            return next(fees)
        except StopIteration:
            raise AssertionError(
                "more melt quotes requested than fee_reserves provided"
            ) from None

    mock_primary_wallet.request_mint = AsyncMock(
        side_effect=lambda amount: Mock(quote=f"mint_quote_{amount}", request=amount)
    )
    mock_token_wallet.melt_quote = AsyncMock(
        side_effect=lambda invoice: Mock(
            quote=f"melt_quote_{invoice}", amount=invoice, fee_reserve=_next_fee()
        )
    )
    mock_token_wallet.melt = AsyncMock(return_value=Mock())

    return mock_token, mock_token_wallet, mock_primary_wallet


@pytest.mark.asyncio
async def test_swap_to_primary_mint_success() -> None:
    """No retry needed: real quote matches the estimate, full net amount minted."""
    from routstr.wallet import swap_to_primary_mint

    mock_token, mock_token_wallet, mock_primary_wallet = _make_swap_mocks(
        1000, fee_reserves=[10, 10]
    )

    from routstr.core.settings import settings

    with patch.object(settings, "primary_mint", "http://primary:3338"):
        with patch.object(settings, "primary_mint_unit", "sat"):
            with patch("routstr.wallet.get_wallet", return_value=mock_primary_wallet):
                amount, unit, mint = await swap_to_primary_mint(
                    mock_token, mock_token_wallet
                )

    assert amount == 990  # 1000 - fee_reserve(10), no buffer subtracted
    assert unit == "sat"
    assert mint == "http://primary:3338"
    assert mock_primary_wallet.request_mint.call_count == 2
    mock_primary_wallet.request_mint.assert_any_call(1000)
    mock_primary_wallet.request_mint.assert_any_call(990)
    assert mock_token_wallet.melt_quote.call_count == 2
    assert mock_token_wallet.melt.call_count == 1
    assert mock_primary_wallet.mint.called


@pytest.mark.asyncio
@pytest.mark.parametrize("fee_reserve", [1, 10, 100])
async def test_calculate_swap_amount_subtracts_only_observed_fees(
    fee_reserve: int,
) -> None:
    """Estimation: minted_amount = token - fee_reserve, with no safety buffer."""
    from routstr.wallet import _calculate_swap_amount

    _, mock_token_wallet, mock_primary_wallet = _make_swap_mocks(
        1000, fee_reserves=[fee_reserve]
    )

    from routstr.core.settings import settings

    with patch.object(settings, "primary_mint", "http://primary:3338"):
        with patch.object(settings, "primary_mint_unit", "sat"):
            result = await _calculate_swap_amount(
                amount_msat=1_000_000,
                token_unit="sat",
                token_mint_url="http://foreign-mint:3338",
                token_wallet=mock_token_wallet,
                primary_wallet=mock_primary_wallet,
                proofs=[],
            )

    assert result == 1000 - fee_reserve


@pytest.mark.asyncio
async def test_calculate_swap_amount_includes_input_fees() -> None:
    """Estimation subtracts NUT-02 input fees alongside the melt fee_reserve."""
    from routstr.wallet import _calculate_swap_amount

    _, mock_token_wallet, mock_primary_wallet = _make_swap_mocks(
        500, fee_reserves=[10], input_fees=3
    )

    from routstr.core.settings import settings

    with patch.object(settings, "primary_mint", "http://primary:3338"):
        with patch.object(settings, "primary_mint_unit", "sat"):
            result = await _calculate_swap_amount(
                amount_msat=500_000,
                token_unit="sat",
                token_mint_url="http://foreign-mint:3338",
                token_wallet=mock_token_wallet,
                primary_wallet=mock_primary_wallet,
                proofs=[],
            )

    assert result == 487  # 500 - 10 - 3


@pytest.mark.asyncio
async def test_swap_retries_when_real_quote_exceeds_estimate() -> None:
    """The real melt quote demands a higher fee than the estimate (20 → 23).
    Instead of failing, the swap recomputes the amount from the observed fee
    and re-quotes: 1000 - 23 = 977, which fits (977 + 23 <= 1000)."""
    from routstr.wallet import swap_to_primary_mint

    mock_token, mock_token_wallet, mock_primary_wallet = _make_swap_mocks(
        1000, fee_reserves=[20, 23, 23]
    )

    from routstr.core.settings import settings

    with patch.object(settings, "primary_mint", "http://primary:3338"):
        with patch.object(settings, "primary_mint_unit", "sat"):
            with patch("routstr.wallet.get_wallet", return_value=mock_primary_wallet):
                amount, unit, mint = await swap_to_primary_mint(
                    mock_token, mock_token_wallet
                )

    assert amount == 977
    assert unit == "sat"
    mock_primary_wallet.request_mint.assert_any_call(980)
    mock_primary_wallet.request_mint.assert_any_call(977)
    assert mock_token_wallet.melt_quote.call_count == 3  # estimation + 2 attempts
    assert mock_token_wallet.melt.call_count == 1


@pytest.mark.asyncio
async def test_swap_retries_when_melt_demands_more_than_quoted() -> None:
    """The mint.cubabitcoin.org incident: every quote reports fee_reserve=1,
    but the mint demands 2 sats at melt time ("Provided: 179, needed: 180").
    The swap must retry with a smaller invoice (177) so the second melt fits,
    instead of failing the topup."""
    from routstr.wallet import swap_to_primary_mint

    mock_token, mock_token_wallet, mock_primary_wallet = _make_swap_mocks(
        179, fee_reserves=[1, 1, 1], mint_url="http://mint.cubabitcoin.org"
    )
    mock_token_wallet.melt.side_effect = [
        Exception(
            "Mint Error: not enough inputs provided for melt. "
            "Provided: 179, needed: 180 (Code: 11000)"
        ),
        Mock(),
    ]

    from routstr.core.settings import settings

    with patch.object(settings, "primary_mint", "http://primary:3338"):
        with patch.object(settings, "primary_mint_unit", "sat"):
            with patch("routstr.wallet.get_wallet", return_value=mock_primary_wallet):
                amount, unit, mint = await swap_to_primary_mint(
                    mock_token, mock_token_wallet
                )

    assert amount == 177  # 179 - 1 (estimate) - 1 (observed melt shortfall)
    assert mock_token_wallet.melt.call_count == 2
    mock_primary_wallet.request_mint.assert_any_call(178)
    mock_primary_wallet.request_mint.assert_any_call(177)


@pytest.mark.asyncio
async def test_swap_retries_on_cdk_unbalanced_error() -> None:
    """cdk-based mints report insufficient melt inputs as the registered code
    11005 (TransactionUnbalanced) with their own message wording — no
    Provided/needed amounts to parse. The retry must classify it by code and
    fall back to shrinking by 1."""
    from routstr.wallet import swap_to_primary_mint

    mock_token, mock_token_wallet, mock_primary_wallet = _make_swap_mocks(
        179, fee_reserves=[1, 1, 1]
    )
    mock_token_wallet.melt.side_effect = [
        Exception("Mint Error: Transaction unbalanced: 179, 178, 2 (Code: 11005)"),
        Mock(),
    ]

    from routstr.core.settings import settings

    with patch.object(settings, "primary_mint", "http://primary:3338"):
        with patch.object(settings, "primary_mint_unit", "sat"):
            with patch("routstr.wallet.get_wallet", return_value=mock_primary_wallet):
                amount, unit, mint = await swap_to_primary_mint(
                    mock_token, mock_token_wallet
                )

    assert amount == 177
    assert mock_token_wallet.melt.call_count == 2


@pytest.mark.asyncio
async def test_swap_quote_retries_exhausted() -> None:
    """A mint that escalates fee_reserve on every re-quote exhausts the retry
    budget (3 attempts) and fails cleanly; melt is never executed."""
    from routstr.wallet import swap_to_primary_mint

    mock_token, mock_token_wallet, mock_primary_wallet = _make_swap_mocks(
        1000, fee_reserves=[1, 10, 25, 50]
    )

    from routstr.core.settings import settings

    with patch.object(settings, "primary_mint", "http://primary:3338"):
        with patch.object(settings, "primary_mint_unit", "sat"):
            with patch("routstr.wallet.get_wallet", return_value=mock_primary_wallet):
                with pytest.raises(ValueError, match="insufficient to cover melt fees"):
                    await swap_to_primary_mint(mock_token, mock_token_wallet)

    assert mock_token_wallet.melt_quote.call_count == 4  # estimation + 3 attempts
    mock_token_wallet.melt.assert_not_called()


@pytest.mark.asyncio
async def test_swap_melt_retries_exhausted() -> None:
    """A mint that always demands more at melt time than it quoted exhausts
    the retry budget; the last melt failure is wrapped as ValueError."""
    from routstr.wallet import swap_to_primary_mint

    mock_token, mock_token_wallet, mock_primary_wallet = _make_swap_mocks(
        5000, fee_reserves=[50, 50, 50, 50]
    )
    mock_token_wallet.melt = AsyncMock(
        side_effect=Exception(
            "Mint Error: not enough inputs provided for melt. "
            "Provided: 5000, needed: 5200 (Code: 11000)"
        )
    )

    from routstr.core.settings import settings

    with patch.object(settings, "primary_mint", "http://primary:3338"):
        with patch.object(settings, "primary_mint_unit", "sat"):
            with patch("routstr.wallet.get_wallet", return_value=mock_primary_wallet):
                with pytest.raises(ValueError, match="Failed to melt token"):
                    await swap_to_primary_mint(mock_token, mock_token_wallet)

    assert mock_token_wallet.melt.call_count == 3


@pytest.mark.asyncio
async def test_swap_does_not_retry_on_payment_failure() -> None:
    """Melt failures unrelated to fees (e.g. routing failure) are not retried:
    a smaller invoice would not help, and the error must surface immediately."""
    from routstr.wallet import swap_to_primary_mint

    mock_token, mock_token_wallet, mock_primary_wallet = _make_swap_mocks(
        1000, fee_reserves=[10, 10]
    )
    mock_token_wallet.melt = AsyncMock(
        side_effect=Exception("Mint Error: Lightning payment failed. (Code: 20004)")
    )

    from routstr.core.settings import settings

    with patch.object(settings, "primary_mint", "http://primary:3338"):
        with patch.object(settings, "primary_mint_unit", "sat"):
            with patch("routstr.wallet.get_wallet", return_value=mock_primary_wallet):
                with pytest.raises(ValueError, match="Failed to melt token"):
                    await swap_to_primary_mint(mock_token, mock_token_wallet)

    assert mock_token_wallet.melt.call_count == 1
    assert mock_primary_wallet.request_mint.call_count == 2
