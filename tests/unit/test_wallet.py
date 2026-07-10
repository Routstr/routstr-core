import asyncio
import base64
import json
import socket
from collections.abc import Generator
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from routstr.core.db import ApiKey
from routstr.wallet import (
    MintConnectionError,
    TokenConsumedError,
    classify_redemption_error,
    credit_balance,
    get_balance,
    is_mint_connection_error,
    recieve_token,
    send_token,
)


@pytest.fixture(autouse=True)
def isolate_wallet_runtime_state() -> Generator[None, None, None]:
    """Keep production limiter/wallet caches from leaking across unit tests."""
    from routstr import wallet as wallet_module
    from routstr.core.settings import settings

    original_rpm = settings.mint_max_requests_per_minute
    settings.mint_max_requests_per_minute = 0
    wallet_module._MintRateLimiter._limiters.clear()
    wallet_module._wallets.clear()
    wallet_module._wallet_last_load.clear()
    wallet_module._wallet_load_locks.clear()
    yield
    settings.mint_max_requests_per_minute = original_rpm
    wallet_module._MintRateLimiter._limiters.clear()
    wallet_module._wallets.clear()
    wallet_module._wallet_last_load.clear()
    wallet_module._wallet_load_locks.clear()


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
    # Fee-free trusted mint (e.g. Minibits): nothing deducted.
    mock_wallet.get_fees_for_proofs = Mock(return_value=0)

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
async def test_recieve_token_trusted_mint_deducts_input_fee() -> None:
    """A trusted mint that charges NUT-02 input fees.

    The same-mint receive (`wallet.split(..., include_fees=True)`, a NUT-03 swap
    at the same mint — not swap_to_primary_mint) pays the mint's per-proof fee,
    so routstr only ends up with `face - input_fee` in fresh proofs. The credited
    amount must reflect that, otherwise routstr over-credits the user and its own
    wallet drifts toward insolvency.
    """
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
    # Mock a 3-sat input fee from the Cashu wallet API.
    mock_wallet.get_fees_for_proofs = Mock(return_value=3)

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
            # Patch get_wallet directly so the module-level `_wallets` cache
            # (keyed by mint URL) can't hand back a wallet from another test.
            with patch(
                "routstr.wallet.get_wallet",
                AsyncMock(return_value=mock_wallet),
            ):
                amount, unit, mint = await recieve_token(token_str)
                assert amount == 997  # 1000 face - 3 sat input fee paid on swap
                assert unit == "sat"
                assert mint == "http://mint:3338"
                mock_wallet.get_fees_for_proofs.assert_called_once_with(
                    mock_token.proofs
                )
                # DLEQ is verified before re-minting the incoming proofs.
                mock_wallet.verify_proofs_dleq.assert_called_once_with(
                    mock_token.proofs
                )


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
    mock_session.exec.return_value.rowcount = 1

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
async def test_credit_balance_rejects_missing_key() -> None:
    """A top-up must fail if the key was pruned after redemption."""
    token_data = {
        "token": [{"mint": "http://mint:3338", "proofs": [{"amount": 1000}]}],
        "unit": "sat",
    }
    token_json = json.dumps(token_data)
    token_b64 = base64.urlsafe_b64encode(token_json.encode()).decode()
    token_str = f"cashuA{token_b64}"

    mock_key = Mock()
    mock_key.balance = 0
    mock_key.hashed_key = "test_hash"
    mock_session = AsyncMock()
    mock_session.exec.return_value.rowcount = 0

    from routstr.core.settings import settings

    with patch.object(settings, "cashu_mints", ["http://mint:3338"]):
        with patch(
            "routstr.wallet.recieve_token",
            return_value=(1000, "sat", "http://mint:3338"),
        ):
            # Post-redemption: token already spent, so a vanished key is a
            # non-retryable TokenConsumedError, not a generic token error.
            with pytest.raises(TokenConsumedError, match="disappeared") as exc_info:
                await credit_balance(token_str, mock_key, mock_session)

    classified = classify_redemption_error(exc_info.value)
    assert classified is not None
    _type, status, _msg, code = classified
    assert (status, code) == (500, "cashu_token_consumed")
    # UPDATE matched nothing; committing would hide the failed credit.
    assert mock_session.exec.called
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
    """Same-mint shortcut: the token is already on the primary mint.

    No cross-mint swap (no melt/mint), but the same-mint split(include_fees=True)
    still burns the mint's NUT-02 input fee, so the credited amount must be face
    minus the input fee — not full face value (the over-credit bug). DLEQ is
    verified too, matching the trusted same-mint receive path.
    """
    from routstr.core.settings import settings
    from routstr.wallet import swap_to_primary_mint

    mock_token = Mock()
    mock_token.mint = settings.primary_mint
    mock_token.keysets = ["keyset1"]
    mock_token.amount = 1000
    mock_token.unit = "sat"
    mock_token.proofs = [{"amount": 1000}]

    mock_token_wallet = Mock()
    mock_token_wallet.load_mint = AsyncMock()
    mock_token_wallet.load_proofs = AsyncMock()
    mock_token_wallet.verify_proofs_dleq = Mock()
    # Mock a 3-sat input fee from the Cashu wallet API.
    mock_token_wallet.get_fees_for_proofs = Mock(return_value=3)
    mock_token_wallet.split = AsyncMock(return_value=None)
    mock_token_wallet.request_mint = AsyncMock()
    mock_token_wallet.melt_quote = AsyncMock()

    with patch("routstr.wallet.get_wallet", AsyncMock(return_value=mock_token_wallet)):
        amount, unit, mint = await swap_to_primary_mint(mock_token, mock_token_wallet)

    assert amount == 997  # 1000 face - 3 sat input fee
    assert unit == "sat"
    assert mint == settings.primary_mint
    mock_token_wallet.verify_proofs_dleq.assert_called_once_with(mock_token.proofs)
    mock_token_wallet.get_fees_for_proofs.assert_called_once_with(mock_token.proofs)
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
@pytest.mark.parametrize(
    "primary_unit,token_unit,amount_msat,fees,expected",
    [
        ("sat", "sat", 179_000, 2, 177),
        ("msat", "sat", 179_000, 2, 177_000),
        ("sat", "msat", 179_000, 2_000, 177),
    ],
)
async def test_net_minted_amount_unit_conversions(
    primary_unit: str, token_unit: str, amount_msat: int, fees: int, expected: int
) -> None:
    """Fee subtraction converts correctly between sat and msat on either side."""
    from routstr.core.settings import settings
    from routstr.wallet import _net_minted_amount

    with patch.object(settings, "primary_mint_unit", primary_unit):
        assert _net_minted_amount(amount_msat, token_unit, fees) == expected


@pytest.mark.parametrize(
    "message,expected",
    [
        # nutshell: retryable with exact shortfall from the detail text
        (
            "Mint Error: not enough inputs provided for melt. "
            "Provided: 179, needed: 182 (Code: 11000)",
            3,
        ),
        # verbatim production error from issue #468, including cashu-py's
        # "could not pay invoice" wrapper around the mint detail
        (
            "could not pay invoice: Mint Error: not enough inputs provided "
            "for melt. Provided: 179, needed: 180 (Code: 11000)",
            1,
        ),
        # cdk: registered TransactionUnbalanced code, no parsable amounts
        ("Mint Error: Transaction unbalanced: 179, 178, 2 (Code: 11005)", 1),
        # nutshell wording without a code suffix
        ("not enough inputs provided for melt", 1),
        # nonsensical amounts (needed <= provided) fall back to the minimal step
        (
            "Mint Error: not enough inputs provided for melt. "
            "Provided: 180, needed: 179 (Code: 11000)",
            1,
        ),
        # a generic 11000 without the shortfall text is not a fee shortfall:
        # 11000 is nutshell's catch-all TransactionError, so retrying (shrinking
        # the invoice) would never help and only masks the real error
        ("Mint Error: Duplicate inputs provided. (Code: 11000)", None),
        # spent proofs must never be retried: the funds are gone
        ("Mint Error: Token already spent. (Code: 11001)", None),
        # Lightning failures must never be retried: a smaller invoice won't help
        ("Mint Error: Lightning payment failed. (Code: 20004)", None),
        # unrecognizable errors (timeouts, bugs) must never be retried
        ("Connection timeout", None),
    ],
)
def test_melt_shortfall_classifier(message: str, expected: int | None) -> None:
    """Retry classification across mint implementations and failure classes."""
    from routstr.wallet import _melt_insufficient_shortfall

    assert _melt_insufficient_shortfall(Exception(message)) == expected


@pytest.mark.asyncio
async def test_calculate_swap_amount_same_mint_short_circuit() -> None:
    """When the token is already on the primary mint no fees apply and no
    quotes are requested."""
    from routstr.wallet import _calculate_swap_amount

    _, mock_token_wallet, mock_primary_wallet = _make_swap_mocks(1000, fee_reserves=[])

    from routstr.core.settings import settings

    with patch.object(settings, "primary_mint", "http://primary:3338"):
        with patch.object(settings, "primary_mint_unit", "sat"):
            result = await _calculate_swap_amount(
                amount_msat=1_000_000,
                token_unit="sat",
                token_mint_url="http://primary:3338",
                token_wallet=mock_token_wallet,
                primary_wallet=mock_primary_wallet,
                proofs=[],
            )

    assert result == 1000
    mock_primary_wallet.request_mint.assert_not_called()
    mock_token_wallet.melt_quote.assert_not_called()


@pytest.mark.asyncio
async def test_calculate_swap_amount_msat_primary_unit() -> None:
    """With an msat primary mint the dummy quote and result stay in msats."""
    from routstr.wallet import _calculate_swap_amount

    _, mock_token_wallet, mock_primary_wallet = _make_swap_mocks(179, fee_reserves=[2])

    from routstr.core.settings import settings

    with patch.object(settings, "primary_mint", "http://primary:3338"):
        with patch.object(settings, "primary_mint_unit", "msat"):
            result = await _calculate_swap_amount(
                amount_msat=179_000,
                token_unit="sat",
                token_mint_url="http://foreign-mint:3338",
                token_wallet=mock_token_wallet,
                primary_wallet=mock_primary_wallet,
                proofs=[],
            )

    assert result == 177_000  # 179_000 msat - 2 sat fee
    mock_primary_wallet.request_mint.assert_called_once_with(179_000)


@pytest.mark.asyncio
async def test_calculate_swap_amount_fees_exceed_token() -> None:
    """Fees larger than the token itself fail fast, before any melt."""
    from routstr.wallet import _calculate_swap_amount

    _, mock_token_wallet, mock_primary_wallet = _make_swap_mocks(
        179, fee_reserves=[200]
    )

    from routstr.core.settings import settings

    with patch.object(settings, "primary_mint", "http://primary:3338"):
        with patch.object(settings, "primary_mint_unit", "sat"):
            with pytest.raises(ValueError, match="exceed token amount"):
                await _calculate_swap_amount(
                    amount_msat=179_000,
                    token_unit="sat",
                    token_mint_url="http://foreign-mint:3338",
                    token_wallet=mock_token_wallet,
                    primary_wallet=mock_primary_wallet,
                    proofs=[],
                )


@pytest.mark.asyncio
async def test_calculate_swap_amount_wraps_estimation_failure() -> None:
    """Estimation infrastructure failures surface as a single clear ValueError."""
    from routstr.wallet import _calculate_swap_amount

    _, mock_token_wallet, mock_primary_wallet = _make_swap_mocks(179, fee_reserves=[])
    mock_primary_wallet.request_mint = AsyncMock(side_effect=Exception("mint offline"))

    from routstr.core.settings import settings

    with patch.object(settings, "primary_mint", "http://primary:3338"):
        with patch.object(settings, "primary_mint_unit", "sat"):
            with pytest.raises(ValueError, match="Failed to estimate fees"):
                await _calculate_swap_amount(
                    amount_msat=179_000,
                    token_unit="sat",
                    token_mint_url="http://foreign-mint:3338",
                    token_wallet=mock_token_wallet,
                    primary_wallet=mock_primary_wallet,
                    proofs=[],
                )


@pytest.mark.asyncio
async def test_swap_coerces_non_integer_amount() -> None:
    """Token amounts arriving as floats are coerced before any arithmetic."""
    from routstr.wallet import swap_to_primary_mint

    mock_token, mock_token_wallet, mock_primary_wallet = _make_swap_mocks(
        1000, fee_reserves=[10, 10]
    )
    mock_token.amount = 1000.0

    from routstr.core.settings import settings

    with patch.object(settings, "primary_mint", "http://primary:3338"):
        with patch.object(settings, "primary_mint_unit", "sat"):
            with patch("routstr.wallet.get_wallet", return_value=mock_primary_wallet):
                amount, unit, mint = await swap_to_primary_mint(
                    mock_token, mock_token_wallet
                )

    assert amount == 990
    assert isinstance(amount, int)


@pytest.mark.asyncio
async def test_swap_rejects_unknown_unit() -> None:
    """Units other than sat/msat are rejected before any quote is requested."""
    from routstr.wallet import swap_to_primary_mint

    mock_token, mock_token_wallet, mock_primary_wallet = _make_swap_mocks(
        1000, fee_reserves=[]
    )
    mock_token.unit = "usd"

    from routstr.core.settings import settings

    with patch.object(settings, "primary_mint", "http://primary:3338"):
        with patch.object(settings, "primary_mint_unit", "sat"):
            with patch("routstr.wallet.get_wallet", return_value=mock_primary_wallet):
                with pytest.raises(ValueError, match="Invalid unit"):
                    await swap_to_primary_mint(mock_token, mock_token_wallet)

    mock_primary_wallet.request_mint.assert_not_called()


@pytest.mark.asyncio
async def test_swap_msat_token_already_on_primary() -> None:
    """msat-denominated tokens on the primary mint short-circuit unchanged."""
    from routstr.wallet import swap_to_primary_mint

    mock_token, mock_token_wallet, _ = _make_swap_mocks(
        179_000, fee_reserves=[], mint_url="http://primary:3338"
    )
    mock_token.unit = "msat"
    mock_token_wallet.split = AsyncMock()

    from routstr.core.settings import settings

    with patch.object(settings, "primary_mint", "http://primary:3338"):
        with patch.object(settings, "primary_mint_unit", "sat"):
            with patch("routstr.wallet.get_wallet", return_value=mock_token_wallet):
                amount, unit, mint = await swap_to_primary_mint(
                    mock_token, mock_token_wallet
                )

    assert (amount, unit, mint) == (179_000, "msat", "http://primary:3338")


# ---------------------------------------------------------------------------
# Mint-on-primary failure handling after a successful melt
#
# At this point the foreign proofs are already spent: failures here mean funds
# are in limbo, so errors must propagate (never be swallowed) and recovery must
# never credit proofs the wallet does not actually hold.
# ---------------------------------------------------------------------------


def _with_recovery_mocks(
    mock_primary_wallet: Mock, mint_error: str, balances: list[int]
) -> None:
    """Make primary mint() fail and stage available_balance per load_proofs call."""
    mock_primary_wallet.mint = AsyncMock(side_effect=Exception(mint_error))
    mock_primary_wallet.keysets = ["keyset_primary"]
    balance_iter = iter(balances)

    def advance_balance(reload: bool = False) -> None:
        mock_primary_wallet.available_balance = Mock(amount=next(balance_iter))

    mock_primary_wallet.load_proofs = AsyncMock(side_effect=advance_balance)
    mock_primary_wallet.restore_tokens_for_keyset = AsyncMock()


@pytest.mark.asyncio
async def test_swap_mint_failure_after_melt_is_token_consumed() -> None:
    """A non-recoverable mint failure after melt is a non-retryable
    TokenConsumedError (the melt already spent the foreign proofs), with the
    original error preserved in the cause chain."""
    from routstr.wallet import swap_to_primary_mint

    mock_token, mock_token_wallet, mock_primary_wallet = _make_swap_mocks(
        1000, fee_reserves=[10, 10]
    )
    _with_recovery_mocks(
        mock_primary_wallet, "Mint Error: Quote is expired (Code: 20007)", [0]
    )

    from routstr.core.settings import settings

    with patch.object(settings, "primary_mint", "http://primary:3338"):
        with patch.object(settings, "primary_mint_unit", "sat"):
            with patch("routstr.wallet.get_wallet", return_value=mock_primary_wallet):
                with pytest.raises(TokenConsumedError) as exc_info:
                    await swap_to_primary_mint(mock_token, mock_token_wallet)

    assert "Quote is expired" in str(exc_info.value.__cause__)
    assert mock_token_wallet.melt.call_count == 1
    mock_primary_wallet.restore_tokens_for_keyset.assert_not_called()


@pytest.mark.asyncio
async def test_swap_recovers_orphaned_proofs_on_outputs_already_signed() -> None:
    """11003 (outputs already signed): a recovery scan that restores the full
    minted amount lets the swap complete normally."""
    from routstr.wallet import swap_to_primary_mint

    mock_token, mock_token_wallet, mock_primary_wallet = _make_swap_mocks(
        1000, fee_reserves=[10, 10]
    )
    _with_recovery_mocks(
        mock_primary_wallet,
        "Mint Error: outputs already signed (Code: 11003)",
        [0, 990],  # pre-mint balance, post-recovery balance
    )

    from routstr.core.settings import settings

    with patch.object(settings, "primary_mint", "http://primary:3338"):
        with patch.object(settings, "primary_mint_unit", "sat"):
            with patch("routstr.wallet.get_wallet", return_value=mock_primary_wallet):
                amount, unit, mint = await swap_to_primary_mint(
                    mock_token, mock_token_wallet
                )

    assert amount == 990
    mock_primary_wallet.restore_tokens_for_keyset.assert_awaited_once_with(
        "keyset_primary", to=1, batch=25
    )


@pytest.mark.asyncio
async def test_swap_recovery_shortfall_refuses_credit() -> None:
    """When the recovery scan restores less than the minted amount, the swap
    must fail rather than credit proofs the wallet does not hold."""
    from routstr.wallet import swap_to_primary_mint

    mock_token, mock_token_wallet, mock_primary_wallet = _make_swap_mocks(
        1000, fee_reserves=[10, 10]
    )
    _with_recovery_mocks(
        mock_primary_wallet,
        "Mint Error: outputs already signed (Code: 11003)",
        [0, 100],  # recovery restores only 100 of the expected 990
    )

    from routstr.core.settings import settings

    with patch.object(settings, "primary_mint", "http://primary:3338"):
        with patch.object(settings, "primary_mint_unit", "sat"):
            with patch("routstr.wallet.get_wallet", return_value=mock_primary_wallet):
                with pytest.raises(TokenConsumedError, match="Swap recovery failed"):
                    await swap_to_primary_mint(mock_token, mock_token_wallet)


@pytest.mark.asyncio
async def test_swap_recovery_failure_wrapped() -> None:
    """When the recovery scan itself fails, the error is wrapped and raised —
    never swallowed."""
    from routstr.wallet import swap_to_primary_mint

    mock_token, mock_token_wallet, mock_primary_wallet = _make_swap_mocks(
        1000, fee_reserves=[10, 10]
    )
    _with_recovery_mocks(
        mock_primary_wallet,
        "Mint Error: outputs already signed (Code: 11003)",
        [0],
    )
    mock_primary_wallet.restore_tokens_for_keyset = AsyncMock(
        side_effect=Exception("wallet db locked")
    )

    from routstr.core.settings import settings

    with patch.object(settings, "primary_mint", "http://primary:3338"):
        with patch.object(settings, "primary_mint_unit", "sat"):
            with patch("routstr.wallet.get_wallet", return_value=mock_primary_wallet):
                with pytest.raises(TokenConsumedError, match="recovery unsuccessful"):
                    await swap_to_primary_mint(mock_token, mock_token_wallet)


@pytest.mark.asyncio
async def test_recieve_token_rejects_multiple_keysets() -> None:
    """Multi-keyset tokens are rejected before touching any wallet."""
    with patch("routstr.wallet.deserialize_token_from_string") as mock_deserialize:
        mock_token = Mock()
        mock_token.keysets = ["keyset1", "keyset2"]
        mock_deserialize.return_value = mock_token

        with pytest.raises(ValueError, match="Multiple keysets"):
            await recieve_token("cashuAmultikeyset")


@pytest.mark.asyncio
async def test_credit_balance_msat_unit_not_converted() -> None:
    """msat-denominated redemptions are credited as-is, without a 1000x."""
    mock_key = Mock()
    mock_key.balance = 0
    mock_key.hashed_key = "test_hash"
    mock_session = AsyncMock()

    from routstr.core.settings import settings

    with patch.object(settings, "cashu_mints", ["http://mint:3338"]):
        with patch(
            "routstr.wallet.recieve_token",
            return_value=(1_000_000, "msat", "http://mint:3338"),
        ):
            amount = await credit_balance("cashuAtest", mock_key, mock_session)

    assert amount == 1_000_000
    assert mock_session.commit.called


@pytest.mark.asyncio
async def test_credit_balance_survives_audit_store_failure() -> None:
    """A failure writing the CashuTransaction history record must not undo the
    already-committed balance credit. (The silent swallow is a known
    audit-trail gap slated for its own fix — this test pins the financial
    invariant that the user keeps their credit, not the swallow itself.)"""
    mock_key = Mock()
    mock_key.balance = 0
    mock_key.hashed_key = "test_hash"
    mock_session = AsyncMock()

    from routstr.core.settings import settings

    with patch.object(settings, "cashu_mints", ["http://mint:3338"]):
        with patch(
            "routstr.wallet.recieve_token",
            return_value=(1000, "sat", "http://mint:3338"),
        ):
            with patch(
                "routstr.wallet.store_cashu_transaction",
                side_effect=Exception("history table locked"),
            ):
                amount = await credit_balance("cashuAtest", mock_key, mock_session)

    assert amount == 1_000_000
    assert mock_session.commit.called


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


# --- Mint-unreachable classification (is_mint_connection_error) ---------------


def _chain(outer: BaseException, cause: BaseException) -> BaseException:
    """Attach ``cause`` as the ``__cause__`` of ``outer`` (as ``raise X from Y``
    would) and return ``outer``."""
    outer.__cause__ = cause
    return outer


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("connection refused"),
        httpx.ConnectTimeout("timed out"),
        httpx.ReadTimeout("read timed out"),  # subclass of TimeoutException
        httpx.PoolTimeout("pool timed out"),
        httpx.WriteError("write failed"),  # subclass of NetworkError
        ConnectionRefusedError("refused"),  # subclass of ConnectionError
        ConnectionResetError("reset"),
        socket.gaierror("Name or service not known"),
        TimeoutError("timed out"),  # asyncio.TimeoutError alias on 3.11+
        MintConnectionError("mint down"),
        # Wrapped: the real transport error survives in the __cause__ chain.
        _chain(ValueError("Failed to estimate fees: boom"), httpx.ConnectError("x")),
        # Two levels deep.
        _chain(
            RuntimeError("outer"),
            _chain(ValueError("mid"), httpx.ConnectTimeout("deep")),
        ),
    ],
)
def test_is_mint_connection_error_detects_transport_failures(
    error: BaseException,
) -> None:
    assert is_mint_connection_error(error) is True


@pytest.mark.parametrize(
    "error",
    [
        ValueError("token already spent"),
        ValueError("Mint unreachable: all connection attempts failed"),  # text only
        ValueError("Invalid Cashu token"),
        # Mint answered with an error status — reachable, so NOT a connection error.
        httpx.HTTPStatusError(
            "500",
            request=httpx.Request("POST", "http://m"),
            response=httpx.Response(500),
        ),
        RuntimeError("some internal fault"),
    ],
)
def test_is_mint_connection_error_ignores_non_transport(error: BaseException) -> None:
    assert is_mint_connection_error(error) is False


def test_is_mint_connection_error_survives_reference_cycle() -> None:
    """A pathological cause/context cycle must not hang the classifier."""
    a = ValueError("a")
    b = ValueError("b")
    a.__cause__ = b
    b.__context__ = a
    assert is_mint_connection_error(a) is False


def test_token_consumed_seals_transport_cause() -> None:
    """A transport error wrapped in TokenConsumedError is NOT retryable — the
    token is spent, so the seal wins over the httpx cause underneath."""
    try:
        raise httpx.ConnectError("mint down")
    except httpx.ConnectError as exc:
        consumed = TokenConsumedError("credit failed")
        consumed.__cause__ = exc

    assert is_mint_connection_error(consumed) is False
    classified = classify_redemption_error(consumed)
    assert classified is not None
    type_, status, _msg, code = classified
    assert (type_, status, code) == ("token_consumed", 500, "cashu_token_consumed")


@pytest.mark.parametrize(
    "error",
    [
        # The message credit_balance raises for a dust/zero redemption.
        ValueError("Redeemed token amount must be positive, got 0 msats"),
        ValueError("Redeemed token amount must be positive, got -5 msats"),
        ValueError("Failed to redeem Cashu token: token yielded no value"),
    ],
)
def test_classify_zero_value(error: ValueError) -> None:
    """A zero/negative redemption gets its own documented code, not the generic
    cashu_token_redemption_failed bucket."""
    classified = classify_redemption_error(error)
    assert classified is not None
    type_, status, _msg, code = classified
    assert (type_, status, code) == ("cashu_error", 400, "cashu_token_zero_value")


def test_classify_generic_valueerror_is_not_zero_value() -> None:
    """A generic wallet ValueError still falls to the generic bucket — the
    zero-value match must not over-trigger."""
    classified = classify_redemption_error(
        ValueError("some unexpected wallet condition")
    )
    assert classified is not None
    type_, status, _msg, code = classified
    assert (type_, status, code) == (
        "cashu_error",
        400,
        "cashu_token_redemption_failed",
    )


@pytest.mark.asyncio
async def test_swap_mint_transport_error_after_melt_is_not_retryable() -> None:
    """A transport error minting on the primary mint (after the foreign melt
    already spent the proofs) classifies as a non-retryable token_consumed 500,
    never a retryable mint_unreachable 503."""
    from routstr.wallet import swap_to_primary_mint

    mock_token, mock_token_wallet, mock_primary_wallet = _make_swap_mocks(
        1000, fee_reserves=[10, 10]
    )
    # Melt succeeds (proofs spent); minting on primary hits a transport error.
    mock_primary_wallet.mint = AsyncMock(
        side_effect=httpx.ConnectError("primary mint down")
    )

    from routstr.core.settings import settings

    with patch.object(settings, "primary_mint", "http://primary:3338"):
        with patch.object(settings, "primary_mint_unit", "sat"):
            with patch("routstr.wallet.get_wallet", return_value=mock_primary_wallet):
                with pytest.raises(TokenConsumedError) as exc_info:
                    await swap_to_primary_mint(mock_token, mock_token_wallet)

    classified = classify_redemption_error(exc_info.value)
    assert classified is not None
    _type, status, _msg, code = classified
    assert status == 500
    assert code == "cashu_token_consumed"
    assert is_mint_connection_error(exc_info.value) is False
    assert mock_token_wallet.melt.call_count == 1


@pytest.mark.asyncio
async def test_credit_balance_db_transport_error_is_token_consumed() -> None:
    """A transport-like DB failure after the token is redeemed must be
    non-retryable (token_consumed), not a retryable mint_unreachable."""
    mock_key = Mock()
    mock_key.balance = 1000
    mock_key.hashed_key = "test_hash"
    mock_session = AsyncMock()
    mock_session.exec = AsyncMock(side_effect=ConnectionError("db connection reset"))

    with patch(
        "routstr.wallet.recieve_token",
        return_value=(100, "sat", "https://mint.example"),
    ):
        with pytest.raises(TokenConsumedError) as exc_info:
            await credit_balance("cashuAtoken", mock_key, mock_session)

    assert is_mint_connection_error(exc_info.value) is False


@pytest.mark.asyncio
async def test_swap_fee_estimation_transport_error_raises_mint_connection_error() -> (
    None
):
    """A transport failure while estimating fees is surfaced as
    MintConnectionError (→ 503), not a generic fee ValueError (→ 422)."""
    from routstr.wallet import swap_to_primary_mint

    mock_token, mock_token_wallet, mock_primary_wallet = _make_swap_mocks(
        1000, fee_reserves=[10]
    )
    mock_primary_wallet.request_mint = AsyncMock(
        side_effect=httpx.ConnectError("All connection attempts failed")
    )

    from routstr.core.settings import settings

    with patch.object(settings, "primary_mint", "http://primary:3338"):
        with patch.object(settings, "primary_mint_unit", "sat"):
            with patch("routstr.wallet.get_wallet", return_value=mock_primary_wallet):
                with pytest.raises(MintConnectionError):
                    await swap_to_primary_mint(mock_token, mock_token_wallet)

    mock_token_wallet.melt.assert_not_called()


@pytest.mark.asyncio
async def test_swap_melt_transport_error_raises_mint_connection_error() -> None:
    """A transport failure during melt is surfaced as MintConnectionError and
    is NOT retried — the mint is down, not demanding higher fees."""
    from routstr.wallet import swap_to_primary_mint

    mock_token, mock_token_wallet, mock_primary_wallet = _make_swap_mocks(
        1000, fee_reserves=[10, 10]
    )
    mock_token_wallet.melt = AsyncMock(side_effect=httpx.ConnectTimeout("timed out"))

    from routstr.core.settings import settings

    with patch.object(settings, "primary_mint", "http://primary:3338"):
        with patch.object(settings, "primary_mint_unit", "sat"):
            with patch("routstr.wallet.get_wallet", return_value=mock_primary_wallet):
                with pytest.raises(MintConnectionError):
                    await swap_to_primary_mint(mock_token, mock_token_wallet)

    assert mock_token_wallet.melt.call_count == 1


# ---------------------------------------------------------------------------
# Per-mint limiter + _mint_operation factory/retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mint_rate_limiter_serializes_waiters_after_refill() -> None:
    from routstr.wallet import _MintRateLimiter

    limiter = _MintRateLimiter("http://mint:3338", 60)
    limiter._tokens = 0
    limiter._last_refill = 0
    clock = {"now": 0.0}
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        clock["now"] += delay
        await real_sleep(0)

    with patch("routstr.wallet.time.monotonic", side_effect=lambda: clock["now"]):
        with patch("routstr.wallet.asyncio.sleep", side_effect=fake_sleep):
            await asyncio.gather(limiter.acquire(), limiter.acquire())

    assert sleeps == pytest.approx([1.0, 1.0])
    assert clock["now"] == pytest.approx(2.0)


def test_mint_rate_limiter_rebuilds_when_setting_changes() -> None:
    from routstr.core.settings import settings
    from routstr.wallet import _MintRateLimiter

    with patch.object(settings, "mint_max_requests_per_minute", 20):
        first = _MintRateLimiter.get("http://mint:3338")
    with patch.object(settings, "mint_max_requests_per_minute", 10):
        second = _MintRateLimiter.get("http://mint:3338")

    assert first is not None
    assert second is not None
    assert first is not second
    assert second._max == 10


@pytest.mark.asyncio
async def test_mint_operation_honors_retry_after_as_minimum() -> None:
    from routstr.core.settings import settings
    from routstr.wallet import _mint_operation

    request = httpx.Request("POST", "http://mint:3338/v1/mint/quote/bolt11")
    response = httpx.Response(429, request=request, headers={"Retry-After": "60"})
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.HTTPStatusError(
                "rate limited", request=request, response=response
            )
        return "ok"

    sleep = AsyncMock()
    with patch.object(settings, "mint_retry_max_attempts", 1):
        with patch.object(settings, "mint_operation_timeout_seconds", 0):
            with patch("routstr.wallet.time.monotonic", return_value=0.1):
                with patch("routstr.wallet.asyncio.sleep", sleep):
                    result = await _mint_operation(factory, mint_url="http://mint:3338")

    assert result == "ok"
    sleep.assert_awaited_once_with(60.0)


@pytest.mark.asyncio
async def test_mint_operation_retries_httpx_timeout_only_when_safe() -> None:
    from routstr.core.settings import settings
    from routstr.wallet import _mint_operation

    retrying = AsyncMock(side_effect=[httpx.ReadTimeout("slow"), "ok"])
    non_retrying = AsyncMock(side_effect=httpx.ReadTimeout("ambiguous"))

    with patch.object(settings, "mint_retry_max_attempts", 2):
        with patch.object(settings, "mint_operation_timeout_seconds", 0):
            with patch("routstr.wallet.asyncio.sleep", AsyncMock()):
                assert await _mint_operation(retrying) == "ok"
                with pytest.raises(httpx.TimeoutException):
                    await _mint_operation(non_retrying, retry_timeouts=False)

    assert retrying.await_count == 2
    assert non_retrying.await_count == 1


@pytest.mark.asyncio
async def test_get_wallet_initializes_and_loads_once_concurrently() -> None:
    from routstr.wallet import get_wallet

    mock_wallet = Mock()
    mock_wallet.load_mint = AsyncMock()
    mock_wallet.load_proofs = AsyncMock()

    with patch(
        "routstr.wallet.Wallet.with_db", AsyncMock(return_value=mock_wallet)
    ) as create:
        with patch("routstr.wallet.time.monotonic", return_value=100.0):
            first, second = await asyncio.gather(
                get_wallet("http://mint:3338"), get_wallet("http://mint:3338")
            )

    assert first is second is mock_wallet
    create.assert_awaited_once()
    mock_wallet.load_mint.assert_awaited_once()
    mock_wallet.load_proofs.assert_awaited_once_with(reload=True)


@pytest.mark.asyncio
async def test_mint_operation_factory_retry_succeeds() -> None:
    """_mint_operation accepts a zero-arg factory, not a dead coroutine.
    A factory that raises twice then succeeds must be retried and return."""
    from routstr.core.settings import settings
    from routstr.wallet import _mint_operation

    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TimeoutError("timeout")
        return "ok"

    with patch.object(settings, "mint_retry_max_attempts", 3):
        with patch.object(settings, "mint_operation_timeout_seconds", 0):
            with patch.object(settings, "mint_max_requests_per_minute", 0):
                with patch("asyncio.sleep", AsyncMock()):
                    result = await _mint_operation(
                        factory, op_name="test_retry", mint_url="http://mint:3338"
                    )

    assert calls == 3
    assert result == "ok"


@pytest.mark.asyncio
async def test_mint_operation_factory_retry_exhausted() -> None:
    """When the factory always times out, _mint_operation raises
    httpx.TimeoutException after mint_retry_max_attempts + 1 attempts."""
    from routstr.core.settings import settings
    from routstr.wallet import _mint_operation

    calls = 0

    async def factory() -> None:
        nonlocal calls
        calls += 1
        raise TimeoutError("always timeout")

    with patch.object(settings, "mint_retry_max_attempts", 2):
        with patch.object(settings, "mint_operation_timeout_seconds", 0):
            with patch.object(settings, "mint_max_requests_per_minute", 0):
                with patch("asyncio.sleep", AsyncMock()):
                    with pytest.raises(httpx.TimeoutException):
                        await _mint_operation(
                            factory, op_name="test_exhaust", mint_url="http://mint:3338"
                        )

    assert calls == 3  # max_attempts(2) + 1


# ---------------------------------------------------------------------------
# Trusted-mint fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lightning_mint_fallback_for_topups() -> None:
    """When the primary mint is unreachable, _request_mint_with_fallback
    falls back to a secondary trusted mint."""
    from routstr.core.settings import settings
    from routstr.lightning import _request_mint_with_fallback

    primary = "http://primary:3338"
    secondary = "http://secondary:3338"

    mock_primary_wallet = Mock()
    mock_primary_wallet.request_mint = AsyncMock(
        side_effect=httpx.ConnectError("primary down")
    )

    mock_quote = Mock()
    mock_quote.request = "lnbc1secondary"
    mock_quote.quote = "quote_secondary"
    mock_secondary_wallet = Mock()
    mock_secondary_wallet.request_mint = AsyncMock(return_value=mock_quote)

    wallets_map = {primary: mock_primary_wallet, secondary: mock_secondary_wallet}
    mock_get = AsyncMock(side_effect=lambda m, *a, **kw: wallets_map[m])

    with patch.object(settings, "primary_mint", primary):
        with patch.object(settings, "cashu_mints", [primary, secondary]):
            with patch.object(settings, "mint_max_requests_per_minute", 0):
                with patch.object(settings, "mint_operation_timeout_seconds", 0):
                    with patch("routstr.lightning.get_wallet", side_effect=mock_get):
                        bolt11, quote_id, mint_url = await _request_mint_with_fallback(
                            1000
                        )

    assert mint_url == secondary
    assert bolt11 == "lnbc1secondary"
    assert quote_id == "quote_secondary"
    mock_primary_wallet.request_mint.assert_called_once()
    mock_secondary_wallet.request_mint.assert_called_once()


@pytest.mark.asyncio
async def test_swap_falls_back_to_secondary_mint() -> None:
    """When the primary mint is unreachable, swap_to_primary_mint falls back
    to a secondary trusted mint as the swap destination."""
    from routstr.core.settings import settings
    from routstr.wallet import _wallet_last_load, _wallets, swap_to_primary_mint

    _wallets.clear()
    _wallet_last_load.clear()

    primary = "http://primary:3338"
    secondary = "http://secondary:3338"
    foreign = "http://foreign:3338"

    mock_token = Mock()
    mock_token.mint = foreign
    mock_token.unit = "sat"
    mock_token.amount = 1000
    mock_token.keysets = ["keyset1"]
    mock_token.proofs = [Mock(amount=1000)]

    mock_token_wallet = Mock()
    mock_token_wallet.load_mint = AsyncMock()
    mock_token_wallet.load_proofs = AsyncMock()
    mock_token_wallet.get_fees_for_proofs = Mock(return_value=0)
    mock_token_wallet.melt_quote = AsyncMock(
        return_value=Mock(quote="melt_q", amount=990, fee_reserve=10)
    )
    mock_token_wallet.melt = AsyncMock(return_value=Mock())

    mock_primary_wallet = Mock()
    mock_primary_wallet.request_mint = AsyncMock(
        side_effect=httpx.ConnectError("primary down")
    )

    mint_quote = Mock(quote="mint_q_secondary", request="lnbc1secondary")
    mock_secondary_wallet = Mock()
    mock_secondary_wallet.load_mint = AsyncMock()
    mock_secondary_wallet.load_proofs = AsyncMock()
    mock_secondary_wallet.available_balance = Mock(amount=0)
    mock_secondary_wallet.keysets = ["ks_secondary"]
    mock_secondary_wallet.restore_tokens_for_keyset = AsyncMock()
    mock_secondary_wallet.request_mint = AsyncMock(return_value=mint_quote)
    mock_secondary_wallet.mint = AsyncMock(return_value=Mock())

    wallets_map = {primary: mock_primary_wallet, secondary: mock_secondary_wallet}
    mock_get = AsyncMock(side_effect=lambda m, *a, **kw: wallets_map[m])

    with patch.object(settings, "primary_mint", primary):
        with patch.object(settings, "primary_mint_unit", "sat"):
            with patch.object(settings, "cashu_mints", [primary, secondary]):
                with patch.object(settings, "mint_max_requests_per_minute", 0):
                    with patch.object(settings, "mint_operation_timeout_seconds", 0):
                        with patch("asyncio.sleep", AsyncMock()):
                            with patch(
                                "routstr.wallet.get_wallet", side_effect=mock_get
                            ):
                                amount, unit, mint_url = await swap_to_primary_mint(
                                    mock_token, mock_token_wallet
                                )

    assert mint_url == secondary
    assert amount == 990  # 1000 - 10 fee_reserve
    assert unit == "sat"
    mock_secondary_wallet.mint.assert_called_once()
    mock_primary_wallet.mint.assert_not_called()


@pytest.mark.asyncio
async def test_lightning_mint_fallback_on_429() -> None:
    """A 429 from the primary mint should trigger fallback to a secondary,
    not just transport errors."""
    from routstr.core.settings import settings
    from routstr.lightning import _request_mint_with_fallback

    primary = "http://primary:3338"
    secondary = "http://secondary:3338"

    mock_resp = Mock(status_code=429, headers={})
    mock_resp.raise_for_status = Mock(
        side_effect=httpx.HTTPStatusError(
            "rate limited", request=Mock(), response=mock_resp
        )
    )
    mock_primary_wallet = Mock()
    mock_primary_wallet.request_mint = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "rate limited", request=Mock(), response=mock_resp
        )
    )

    mock_quote = Mock(request="lnbc1secondary", quote="quote_secondary")
    mock_secondary_wallet = Mock()
    mock_secondary_wallet.request_mint = AsyncMock(return_value=mock_quote)

    wallets_map = {primary: mock_primary_wallet, secondary: mock_secondary_wallet}
    mock_get = AsyncMock(side_effect=lambda m, *a, **kw: wallets_map[m])

    with patch.object(settings, "primary_mint", primary):
        with patch.object(settings, "cashu_mints", [primary, secondary]):
            with patch.object(settings, "mint_retry_max_attempts", 0):
                with patch.object(settings, "mint_max_requests_per_minute", 0):
                    with patch.object(settings, "mint_operation_timeout_seconds", 0):
                        with patch(
                            "routstr.lightning.get_wallet", side_effect=mock_get
                        ):
                            (
                                bolt11,
                                quote_id,
                                mint_url,
                            ) = await _request_mint_with_fallback(1000)

    assert mint_url == secondary
    mock_secondary_wallet.request_mint.assert_called_once()


@pytest.mark.asyncio
async def test_lightning_mint_fallback_all_fail() -> None:
    """When every trusted mint fails, _request_mint_with_fallback raises
    MintConnectionError instead of trying indefinitely."""
    from routstr.core.settings import settings
    from routstr.lightning import _request_mint_with_fallback
    from routstr.wallet import MintConnectionError

    primary = "http://primary:3338"
    secondary = "http://secondary:3338"

    mock_primary_wallet = Mock()
    mock_primary_wallet.request_mint = AsyncMock(side_effect=httpx.ConnectError("down"))
    mock_secondary_wallet = Mock()
    mock_secondary_wallet.request_mint = AsyncMock(
        side_effect=httpx.ConnectError("down")
    )

    wallets_map = {primary: mock_primary_wallet, secondary: mock_secondary_wallet}
    mock_get = AsyncMock(side_effect=lambda m, *a, **kw: wallets_map[m])

    with patch.object(settings, "primary_mint", primary):
        with patch.object(settings, "cashu_mints", [primary, secondary]):
            with patch.object(settings, "mint_retry_max_attempts", 0):
                with patch.object(settings, "mint_max_requests_per_minute", 0):
                    with patch.object(settings, "mint_operation_timeout_seconds", 0):
                        with patch(
                            "routstr.lightning.get_wallet", side_effect=mock_get
                        ):
                            with pytest.raises(MintConnectionError):
                                await _request_mint_with_fallback(1000)
