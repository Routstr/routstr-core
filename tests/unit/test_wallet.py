import asyncio
import base64
import json
import socket
from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import httpx
import pytest

from routstr.core.db import ApiKey
from routstr.wallet import (
    Bolt11PaymentAmbiguous,
    Bolt11PaymentNotAttempted,
    MintConnectionError,
    TokenConsumedError,
    UntrustedSourceMintError,
    _is_mint_rate_limited,
    classify_redemption_error,
    credit_balance,
    execute_bolt11_payment,
    get_balance,
    is_mint_connection_error,
    prepare_bolt11_payment,
    recieve_token,
    send,
    send_token,
    send_token_from_owner_locked,
)


@pytest.fixture(autouse=True)
def isolate_wallet_runtime_state() -> Generator[None, None, None]:
    """Keep production limiter/wallet caches from leaking across unit tests."""
    from routstr import wallet as wallet_module
    from routstr.core.settings import settings

    original_concurrency = settings.mint_max_concurrency
    settings.mint_max_concurrency = 0
    wallet_module._MintRateGuard._guards.clear()
    wallet_module._wallets.clear()
    wallet_module._wallet_last_load.clear()
    wallet_module._wallet_last_mint_load.clear()
    wallet_module._wallet_load_locks.clear()
    wallet_module._mint_metadata_last_load.clear()
    wallet_module._mint_metadata_load_locks.clear()
    yield
    settings.mint_max_concurrency = original_concurrency
    wallet_module._MintRateGuard._guards.clear()
    wallet_module._wallets.clear()
    wallet_module._wallet_last_load.clear()
    wallet_module._wallet_last_mint_load.clear()
    wallet_module._wallet_load_locks.clear()
    wallet_module._mint_metadata_last_load.clear()
    wallet_module._mint_metadata_load_locks.clear()


@pytest.mark.asyncio
async def test_get_balance() -> None:
    mock_wallet = Mock()
    mock_wallet.available_balance = Mock(amount=50000)
    mock_wallet.load_mint = AsyncMock()
    mock_wallet.load_proofs = AsyncMock()

    # Reset the module-level wallet cache so a real wallet cached by an earlier
    # test (e.g. an unmocked admin-withdraw path) can't shadow the mock here.
    with (
        patch("routstr.wallet._wallets", {}),
        patch("routstr.wallet.Wallet.with_db", return_value=mock_wallet),
    ):
        balance = await get_balance("sat")
        assert balance == 50000


@pytest.mark.asyncio
async def test_wallet_metadata_is_reused_across_units() -> None:
    from routstr.wallet import Wallet

    sat_wallet = MagicMock(url="http://mint:3338")
    sat_wallet.load_mint_keysets = AsyncMock()
    sat_wallet.activate_keyset = AsyncMock()
    sat_wallet.load_mint_info = AsyncMock()
    sat_wallet.load_keysets_from_db = AsyncMock()

    msat_wallet = MagicMock(url="http://mint:3338")
    msat_wallet.load_mint_keysets = AsyncMock()
    msat_wallet.activate_keyset = AsyncMock()
    msat_wallet.load_mint_info = AsyncMock()
    msat_wallet.load_keysets_from_db = AsyncMock()

    with patch("routstr.wallet.time.monotonic", return_value=1000.0):
        await Wallet.load_mint(sat_wallet)
        await Wallet.load_mint(msat_wallet)

    sat_wallet.load_mint_keysets.assert_awaited_once_with(False)
    sat_wallet.load_mint_info.assert_awaited_once_with(reload=True)
    msat_wallet.load_mint_keysets.assert_not_awaited()
    msat_wallet.load_keysets_from_db.assert_awaited_once_with()
    msat_wallet.load_mint_info.assert_awaited_once_with(reload=False)


@pytest.mark.asyncio
async def test_get_wallet_refreshes_local_proofs_without_reloading_mint() -> None:
    from routstr import wallet as wallet_module
    from routstr.wallet import get_wallet

    mock_wallet = Mock(load_mint=AsyncMock(), load_proofs=AsyncMock())
    with (
        patch("routstr.wallet.Wallet.with_db", AsyncMock(return_value=mock_wallet)),
        patch("routstr.wallet.time.monotonic", return_value=1000.0),
    ):
        await get_wallet("http://mint:3338", "sat")
        wallet_module._wallet_last_load["http://mint:3338_sat"] = 900.0
        await get_wallet("http://mint:3338", "sat")

    assert mock_wallet.load_mint.await_count == 1
    assert mock_wallet.load_proofs.await_count == 2


@pytest.mark.asyncio
async def test_get_wallet_quote_only_skips_proof_reload() -> None:
    from routstr.wallet import get_wallet

    mock_wallet = Mock(load_mint=AsyncMock(), load_proofs=AsyncMock())
    with patch("routstr.wallet.Wallet.with_db", AsyncMock(return_value=mock_wallet)):
        await get_wallet("http://mint:3338", "sat", load_proofs=False)

    mock_wallet.load_mint.assert_awaited_once_with()
    mock_wallet.load_proofs.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_wallet_force_reload_bypasses_reload_interval() -> None:
    from routstr.wallet import get_wallet

    mock_wallet = Mock(load_mint=AsyncMock(), load_proofs=AsyncMock())
    with patch("routstr.wallet.Wallet.with_db", AsyncMock(return_value=mock_wallet)):
        await get_wallet("http://mint:3338", "sat")
        await get_wallet("http://mint:3338", "sat", force_reload=True)

    assert mock_wallet.load_mint.await_count == 2
    assert mock_wallet.load_proofs.await_count == 2


@pytest.mark.asyncio
async def test_public_recieve_token_holds_wallet_operation_guard() -> None:
    inside_guard = False

    @asynccontextmanager
    async def operation_guard() -> AsyncIterator[None]:
        nonlocal inside_guard
        inside_guard = True
        try:
            yield
        finally:
            inside_guard = False

    async def receive_locked(*_args: object, **_kwargs: object) -> tuple[int, str, str]:
        assert inside_guard
        return 1, "sat", "https://mint.example"

    with (
        patch("routstr.wallet.wallet_operation_guard", operation_guard),
        patch("routstr.wallet._recieve_token_locked", side_effect=receive_locked),
    ):
        assert await recieve_token("cashuAtoken") == (
            1,
            "sat",
            "https://mint.example",
        )

    assert inside_guard is False


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

            mock_wallet.load_mint_keysets = AsyncMock()
            mock_wallet.activate_keyset = AsyncMock()
            mock_wallet._expand_short_keyset_ids = AsyncMock()
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
    at the same mint) pays the mint's per-proof fee,
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

            mock_wallet.load_mint_keysets = AsyncMock()
            mock_wallet.activate_keyset = AsyncMock()
            mock_wallet._expand_short_keyset_ids = AsyncMock()
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
async def test_recieve_token_redeems_on_issuing_mint_never_swaps() -> None:
    """A token from a secondary trusted mint stays on that mint even though a
    different primary mint is configured; no cross-mint swap is attempted."""
    from routstr.core.settings import settings

    source = "http://secondary:3338"
    primary = "http://primary:3338"
    token = Mock(
        mint=source,
        unit="sat",
        amount=100,
        keysets=["keyset1"],
        proofs=[Mock(amount=100)],
    )
    source_wallet = Mock()
    redeem = AsyncMock(return_value=(99, "sat", source))

    with (
        patch.object(settings, "primary_mint", primary),
        patch.object(settings, "cashu_mints", [primary, source]),
        patch("routstr.wallet.deserialize_token_from_string", return_value=token),
        patch("routstr.wallet.get_wallet", AsyncMock(return_value=source_wallet)),
        patch("routstr.wallet._redeem_same_mint", redeem),
    ):
        result = await recieve_token("cashuAtoken", destination_unit="sat")

    assert result == (99, "sat", source)
    redeem.assert_awaited_once_with(source_wallet, token)


@pytest.mark.asyncio
async def test_recieve_token_rejects_unit_mismatch_before_wallet_mutation() -> None:
    token = Mock(mint="http://key-mint:3338", unit="msat", keysets=["keyset"])
    get_wallet = AsyncMock()

    with (
        patch("routstr.wallet.settings.cashu_mints", ["http://key-mint:3338"]),
        patch("routstr.wallet.deserialize_token_from_string", return_value=token),
        patch("routstr.wallet.get_wallet", get_wallet),
        pytest.raises(ValueError, match="liability unit"),
    ):
        await recieve_token("cashuAtoken", destination_unit="sat")

    get_wallet.assert_not_awaited()


@pytest.mark.asyncio
async def test_primary_mint_failure_does_not_try_another_mint() -> None:
    from routstr.core.settings import settings
    from routstr.wallet import SourceMintConnectionError

    source = "http://primary:3338"
    destination = "http://secondary:3338"
    token = Mock(
        mint=source,
        unit="sat",
        amount=100,
        keysets=["keyset1"],
        proofs=[Mock(amount=100)],
    )
    source_wallet = Mock(
        load_mint_keysets=AsyncMock(side_effect=httpx.ConnectError("mint unavailable")),
        activate_keyset=AsyncMock(),
    )
    get_wallet = AsyncMock(return_value=source_wallet)

    with (
        patch.object(settings, "primary_mint", source),
        patch.object(settings, "cashu_mints", [source, destination]),
        patch("routstr.wallet.deserialize_token_from_string", return_value=token),
        patch("routstr.wallet.get_wallet", get_wallet),
        patch("routstr.wallet.logger.warning") as warning,
    ):
        with pytest.raises(SourceMintConnectionError):
            await recieve_token("cashuAtoken")

    get_wallet.assert_awaited_once_with(source, "sat", load=False)
    failure = next(
        call.kwargs["extra"]
        for call in warning.call_args_list
        if call.kwargs.get("extra", {}).get("event")
        == "cashu_same_mint_redemption_failed"
    )
    assert failure["cross_mint_fallback_attempted"] is False
    assert failure["action"] == "retry_with_token_from_another_mint"


@pytest.mark.asyncio
async def test_same_mint_split_timeout_is_non_retryable() -> None:
    from routstr.wallet import _redeem_same_mint

    token = Mock(
        keysets=["keyset1"],
        mint="http://mint:3338",
        unit="sat",
        amount=1000,
        proofs=[Mock(amount=1000)],
    )
    wallet = Mock(
        load_mint_keysets=AsyncMock(),
        activate_keyset=AsyncMock(),
        _expand_short_keyset_ids=AsyncMock(),
        split=AsyncMock(side_effect=httpx.ReadTimeout("response lost")),
        get_fees_for_proofs=Mock(return_value=0),
    )

    with pytest.raises(TokenConsumedError, match="outcome is ambiguous") as caught:
        await _redeem_same_mint(wallet, token)

    classified = classify_redemption_error(caught.value)
    assert classified is not None
    assert classified[0] == "token_consumed"
    assert classified[1] == 500
    assert classified[3] == "cashu_token_consumed"


@pytest.mark.asyncio
async def test_same_mint_split_connect_error_remains_retryable() -> None:
    from routstr.wallet import SourceMintConnectionError, _redeem_same_mint

    token = Mock(
        keysets=["keyset1"],
        mint="http://mint:3338",
        unit="sat",
        amount=1000,
        proofs=[Mock(amount=1000)],
    )
    wallet = Mock(
        load_mint_keysets=AsyncMock(),
        activate_keyset=AsyncMock(),
        _expand_short_keyset_ids=AsyncMock(),
        split=AsyncMock(side_effect=httpx.ConnectError("connect failed")),
        get_fees_for_proofs=Mock(return_value=0),
    )

    with pytest.raises(SourceMintConnectionError):
        await _redeem_same_mint(wallet, token)


@pytest.mark.asyncio
async def test_send_token() -> None:
    mock_wallet = Mock()

    with patch("routstr.wallet.Wallet.with_db", return_value=mock_wallet):
        with patch("routstr.wallet.send", return_value=(1000, "test_token")):
            token = await send_token(1000, "sat", "http://mint:3338")
            assert token == "test_token"


@pytest.mark.asyncio
async def test_owner_only_token_rejects_customer_backed_proofs() -> None:
    mint = "http://mint:3338"
    proof = Mock(amount=1000, reserved=False)
    wallet = Mock(keysets={}, proofs=[proof], select_to_send=AsyncMock())

    with (
        patch(
            "routstr.wallet.find_trusted_mint_with_funds",
            AsyncMock(return_value=mint),
        ),
        patch("routstr.wallet.get_wallet", AsyncMock(return_value=wallet)),
        patch(
            "routstr.wallet.get_proofs_per_mint_and_unit",
            return_value=[proof],
        ),
        patch(
            "routstr.wallet._owner_balance_for_mint_and_unit",
            AsyncMock(return_value=50),
        ),
        pytest.raises(ValueError, match="Owner Cashu balance"),
    ):
        await send_token_from_owner_locked(100, "sat", mint)

    wallet.select_to_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_release_token_reservation_unreserves_local_proofs() -> None:
    from routstr.wallet import release_token_reservation

    token_proof = Mock(secret="proof-secret", reserved=True)
    cached_proof = Mock(secret="proof-secret", reserved=True)
    token = Mock(mint="http://mint:3338", unit="sat", proofs=[token_proof])
    wallet = Mock(
        proofs=[cached_proof],
        load_proofs=AsyncMock(),
        set_reserved_for_send=AsyncMock(),
    )
    with (
        patch("routstr.wallet.deserialize_token_from_string", return_value=token),
        patch(
            "routstr.wallet.get_wallet", AsyncMock(return_value=wallet)
        ) as get_wallet,
    ):
        await release_token_reservation("cashu-token")

    get_wallet.assert_awaited_once_with("http://mint:3338", "sat", load=False)
    wallet.load_proofs.assert_awaited_once_with(reload=True)
    wallet.set_reserved_for_send.assert_awaited_once_with(token.proofs, reserved=False)
    assert token_proof.reserved is False
    assert cached_proof.reserved is False


@pytest.mark.asyncio
async def test_refund_mint_falls_back_to_trusted_mint_with_funds() -> None:
    from routstr.core.settings import settings
    from routstr.wallet import find_trusted_mint_with_funds

    primary = "http://primary:3338"
    secondary = "http://secondary:3338"

    def wallet_for(mint: str, amount: int) -> Mock:
        keyset = Mock(id=f"keyset-{mint}", mint_url=mint)
        keyset.unit.name = "sat"
        proof = Mock(id=keyset.id, amount=amount, reserved=False)
        return Mock(keysets={keyset.id: keyset}, proofs=[proof])

    wallets = {
        primary: wallet_for(primary, 50),
        secondary: wallet_for(secondary, 200),
    }
    with (
        patch.object(settings, "primary_mint", primary),
        patch.object(settings, "cashu_mints", [primary, secondary]),
        patch(
            "routstr.wallet.get_wallet",
            AsyncMock(side_effect=lambda mint, *args, **kwargs: wallets[mint]),
        ),
    ):
        mint = await find_trusted_mint_with_funds(100, "sat", primary)

    assert mint == secondary


@pytest.mark.asyncio
async def test_send_refreshes_reservations_inside_wallet_guard() -> None:
    mint = "http://mint:3338"
    proof = Mock(amount=1000, reserved=False)
    wallet = Mock(
        keysets={},
        proofs=[proof],
        select_to_send=AsyncMock(return_value=([proof], None)),
        serialize_proofs=AsyncMock(return_value="token"),
        set_reserved_for_send=AsyncMock(),
    )
    inside_guard = False

    @asynccontextmanager
    async def operation_guard() -> AsyncIterator[None]:
        nonlocal inside_guard
        inside_guard = True
        try:
            yield
        finally:
            inside_guard = False

    async def find_mint(
        amount: int,
        unit: str,
        preferred_mint: str | None,
        *,
        force_reload: bool,
    ) -> str:
        assert inside_guard
        assert (amount, unit, preferred_mint, force_reload) == (
            1000,
            "sat",
            mint,
            True,
        )
        return mint

    async def get_loaded_wallet(*_: object, **__: object) -> Mock:
        assert inside_guard
        return wallet

    with (
        patch("routstr.wallet.wallet_operation_guard", operation_guard),
        patch("routstr.wallet.find_trusted_mint_with_funds", side_effect=find_mint),
        patch("routstr.wallet.get_wallet", side_effect=get_loaded_wallet),
        patch(
            "routstr.wallet.get_proofs_per_mint_and_unit",
            return_value=[proof],
        ),
    ):
        assert await send(1000, "sat", mint) == (1000, "token")

    wallet.set_reserved_for_send.assert_awaited_once_with([proof], reserved=True)


@pytest.mark.asyncio
async def test_send_falls_back_when_preferred_mint_has_only_reserved_balance() -> None:
    from routstr.core.settings import settings

    preferred = "http://preferred:3338"
    primary = "http://primary:3338"
    preferred_wallet = Mock(keysets={}, proofs=[])
    preferred_wallet.select_to_send = AsyncMock()
    primary_wallet = Mock(keysets={}, proofs=[])
    primary_wallet.select_to_send = AsyncMock()
    primary_wallet.serialize_proofs = AsyncMock(return_value="primary-token")
    primary_wallet.set_reserved_for_send = AsyncMock()

    preferred_liquid = Mock(amount=500, reserved=False)
    preferred_reserved = Mock(amount=600, reserved=True)
    primary_liquid = Mock(amount=1000, reserved=False)
    primary_wallet.select_to_send.return_value = ([primary_liquid], None)

    async def get_wallet(mint_url: str, unit: str, **_: object) -> Mock:
        assert unit == "sat"
        return primary_wallet if mint_url == primary else preferred_wallet

    def get_proofs(
        wallet: Mock,
        mint_url: str,
        unit: str,
        *,
        not_reserved: bool = False,
    ) -> list[Mock]:
        assert unit == "sat"
        if wallet is primary_wallet:
            assert mint_url == primary
            proofs = [primary_liquid]
        else:
            assert mint_url == preferred
            proofs = [preferred_liquid, preferred_reserved]
        return (
            [proof for proof in proofs if not proof.reserved]
            if not_reserved
            else proofs
        )

    with (
        patch.object(settings, "primary_mint", primary),
        patch.object(settings, "cashu_mints", [primary, preferred]),
        patch("routstr.wallet.get_wallet", side_effect=get_wallet),
        patch("routstr.wallet.get_proofs_per_mint_and_unit", side_effect=get_proofs),
    ):
        amount, token = await send(1000, "sat", preferred)

    assert (amount, token) == (1000, "primary-token")
    preferred_wallet.select_to_send.assert_not_awaited()
    primary_wallet.select_to_send.assert_awaited_once_with(
        [primary_liquid], 1000, set_reserved=False, include_fees=False
    )


@pytest.mark.asyncio
async def test_send_primary_with_only_reserved_proofs_still_raises() -> None:
    from routstr.core.settings import settings

    primary = "http://primary:3338"
    wallet = Mock(keysets={}, proofs=[])
    wallet.select_to_send = AsyncMock()
    reserved = Mock(amount=1000, reserved=True)

    def get_proofs(
        _wallet: Mock,
        _mint_url: str,
        _unit: str,
        *,
        not_reserved: bool = False,
    ) -> list[Mock]:
        return [] if not_reserved else [reserved]

    with (
        patch.object(settings, "primary_mint", primary),
        patch.object(settings, "cashu_mints", [primary]),
        patch("routstr.wallet.get_wallet", AsyncMock(return_value=wallet)),
        patch("routstr.wallet.get_proofs_per_mint_and_unit", side_effect=get_proofs),
        pytest.raises(ValueError, match="No trusted mint has"),
    ):
        await send(1000, "sat", primary)

    wallet.select_to_send.assert_not_awaited()


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
            with patch("routstr.wallet.store_cashu_transaction", AsyncMock()):
                amount = await credit_balance(token_str, mock_key, mock_session)
            assert amount == 1000000  # converted to msat
            assert mock_key.balance == 6000000  # Should be updated after refresh
            # Verify atomic operations were used
            assert mock_session.exec.called  # Atomic UPDATE statement
            assert mock_session.commit.called
            assert mock_session.refresh.called


@pytest.mark.asyncio
async def test_concurrent_duplicate_token_credits_exactly_once() -> None:
    key = Mock(balance=0, hashed_key="duplicate-key")
    session = AsyncMock()
    session.exec.return_value.rowcount = 1
    session.refresh = AsyncMock()
    receive = AsyncMock(
        side_effect=[
            (1000, "sat", "https://mint.test"),
            ValueError("Mint Error: proofs already spent (Code: 11001)"),
        ]
    )
    store = AsyncMock()

    with (
        patch("routstr.wallet.recieve_token", receive),
        patch("routstr.wallet.store_cashu_transaction", store),
    ):
        results = await asyncio.gather(
            credit_balance("cashuAduplicate", key, session),
            credit_balance("cashuAduplicate", key, session),
            return_exceptions=True,
        )

    assert sum(result == 1_000_000 for result in results) == 1
    failure = next(result for result in results if isinstance(result, Exception))
    classified = classify_redemption_error(failure)
    assert classified is not None and classified[3] == "cashu_token_already_spent"
    assert session.exec.await_count == 1
    store.assert_awaited_once()


@pytest.mark.asyncio
async def test_credit_balance_redeems_on_token_mint_not_key_mint() -> None:
    """Top-ups are redeemed where the token was issued; the key's bound mint is
    only a refund preference, never a swap destination."""
    key_mint = "http://key-mint:3338"
    mock_key = Mock(
        balance=1_000_000,
        hashed_key="test_hash",
        refund_mint_url=key_mint,
        refund_currency="sat",
    )
    mock_session = AsyncMock()
    mock_session.exec.return_value.rowcount = 1
    receive = AsyncMock(return_value=(1000, "sat", key_mint))

    with patch("routstr.wallet.recieve_token", receive):
        with patch("routstr.wallet.store_cashu_transaction", AsyncMock()):
            await credit_balance("cashuAtoken", mock_key, mock_session)

    receive.assert_awaited_once_with("cashuAtoken", destination_unit="sat")


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
async def test_recieve_token_untrusted_mint() -> None:
    mock_wallet = Mock()

    with patch("routstr.wallet.deserialize_token_from_string") as mock_deserialize:
        mock_token = Mock()
        mock_token.keysets = ["keyset1"]
        mock_token.mint = "http://untrusted:3338"
        mock_token.unit = "sat"
        mock_token.amount = 1000
        mock_deserialize.return_value = mock_token

        with_db = AsyncMock(return_value=mock_wallet)
        with patch("routstr.wallet.Wallet.with_db", with_db):
            with pytest.raises(UntrustedSourceMintError):
                await recieve_token("test_token")
        with_db.assert_not_awaited()


@pytest.mark.asyncio
async def test_recieve_token_rejects_multiple_keysets() -> None:
    """Multi-keyset tokens are rejected before touching any wallet."""
    from routstr.core.settings import settings

    with patch("routstr.wallet.deserialize_token_from_string") as mock_deserialize:
        mock_token = Mock()
        mock_token.mint = settings.primary_mint
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
            with patch("routstr.wallet.store_cashu_transaction", AsyncMock()):
                amount = await credit_balance("cashuAtest", mock_key, mock_session)

    assert amount == 1_000_000
    assert mock_session.commit.called


@pytest.mark.asyncio
async def test_credit_balance_propagates_audit_store_failure_after_credit() -> None:
    """A final transaction-history failure propagates after committing credit."""
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
                with pytest.raises(Exception, match="history table locked"):
                    await credit_balance("cashuAtest", mock_key, mock_session)

    assert mock_session.commit.called


# --- Mint-unreachable classification (is_mint_connection_error) ---------------


def _chain(outer: BaseException, cause: BaseException) -> BaseException:
    """Attach ``cause`` as the ``__cause__`` of ``outer`` (as ``raise X from Y``
    would) and return ``outer``."""
    outer.__cause__ = cause
    return outer


def test_rate_limited_mint_is_classified_as_unreachable() -> None:
    from routstr.wallet import classify_redemption_error

    request = httpx.Request("POST", "http://mint:3338/v1/swap")
    response = httpx.Response(429, request=request)
    error = httpx.HTTPStatusError("rate limited", request=request, response=response)

    assert classify_redemption_error(error) == (
        "mint_rate_limited",
        503,
        "Cashu mint is rate-limiting requests; retry later",
        "cashu_mint_rate_limited",
    )


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
async def test_execute_bolt11_payment_rejects_unpaid_melt_state() -> None:
    plan = MagicMock()
    plan.proofs = [MagicMock(amount=110)]
    plan.quote.amount = 100
    plan.quote.fee_reserve = 10
    plan.quote.quote = "quote-1"
    plan.invoice = "lnbc-invoice"
    plan.wallet.select_to_send = AsyncMock(return_value=(plan.proofs, 0))
    plan.wallet.set_reserved_for_send = AsyncMock()
    plan.wallet.melt = AsyncMock(return_value=MagicMock(state="UNPAID", change=[]))

    with pytest.raises(Bolt11PaymentNotAttempted):
        await execute_bolt11_payment(plan)

    # An explicit unpaid answer means the proofs are ours again.
    plan.wallet.set_reserved_for_send.assert_awaited_with(plan.proofs, reserved=False)


@pytest.mark.asyncio
async def test_execute_bolt11_payment_accepts_legacy_paid_response() -> None:
    plan = MagicMock()
    plan.proofs = [MagicMock(amount=110)]
    plan.quote.amount = 100
    plan.quote.fee_reserve = 10
    plan.quote.quote = "quote-1"
    plan.invoice = "lnbc-invoice"
    plan.mint_url = "https://mint.test"
    plan.unit = "sat"
    plan.wallet.select_to_send = AsyncMock(return_value=(plan.proofs, 0))
    plan.wallet.set_reserved_for_send = AsyncMock()
    plan.wallet.melt = AsyncMock(
        return_value=MagicMock(state=None, paid=True, change=[])
    )

    assert await execute_bolt11_payment(plan) == (
        110,
        "https://mint.test",
        "sat",
    )


@pytest.mark.asyncio
async def test_execute_bolt11_payment_keeps_proofs_reserved_when_melt_errors() -> None:
    plan = MagicMock()
    plan.proofs = [MagicMock(amount=110)]
    plan.quote.amount = 100
    plan.quote.fee_reserve = 10
    plan.quote.quote = "quote-1"
    plan.invoice = "lnbc-invoice"
    plan.wallet.select_to_send = AsyncMock(return_value=(plan.proofs, 0))
    plan.wallet.set_reserved_for_send = AsyncMock()
    plan.wallet.set_reserved_for_melt = AsyncMock()
    plan.wallet.melt = AsyncMock(side_effect=TimeoutError("no answer"))

    with pytest.raises(Bolt11PaymentAmbiguous):
        await execute_bolt11_payment(plan)

    # The mint may still settle with these proofs. cashu's own melt()
    # un-reserves them on a mint transport error, so the ambiguous path must
    # re-reserve — and it must do so with the melt quote id, because
    # get_melt_quote() finds the proofs to settle by melt_id.
    plan.wallet.set_reserved_for_melt.assert_awaited_once_with(
        plan.proofs, reserved=True, quote_id="quote-1"
    )


@pytest.mark.asyncio
async def test_execute_bolt11_payment_does_not_reserve_when_selection_fails() -> None:
    plan = MagicMock()
    plan.proofs = [MagicMock(amount=110)]
    plan.quote.amount = 100
    plan.quote.fee_reserve = 10
    plan.wallet.select_to_send = AsyncMock(side_effect=ValueError("insufficient"))
    plan.wallet.set_reserved_for_send = AsyncMock()
    plan.wallet.melt = AsyncMock()

    with pytest.raises(Bolt11PaymentNotAttempted):
        await execute_bolt11_payment(plan)

    plan.wallet.set_reserved_for_send.assert_not_awaited()
    plan.wallet.melt.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepare_bolt11_payment_counts_input_fees_in_sufficiency() -> None:
    from routstr.core.settings import settings

    wallet = MagicMock()
    wallet.proofs = [MagicMock(amount=105)]
    wallet.melt_quote = AsyncMock(
        return_value=MagicMock(amount=100, fee_reserve=2, quote="quote-1")
    )
    # Balance covers amount + fee_reserve (102) but not the 5 sat input fee.
    wallet.get_fees_for_proofs = Mock(return_value=5)

    async def get_wallet(mint_url: str, unit: str = "sat", **_: object) -> MagicMock:
        if unit == "msat":
            raise ValueError("unit unsupported")
        return wallet

    with (
        patch.object(settings, "cashu_mints", ["https://only.test"]),
        patch.object(settings, "primary_mint", "https://only.test"),
        patch("routstr.wallet.get_wallet", side_effect=get_wallet),
        patch(
            "routstr.wallet.get_proofs_per_mint_and_unit",
            side_effect=lambda wallet, *args, **kwargs: wallet.proofs,
        ),
        patch(
            "routstr.wallet.slow_filter_spend_proofs",
            side_effect=lambda proofs, wallet: proofs,
        ),
        pytest.raises(ValueError, match="enough balance"),
    ):
        await prepare_bolt11_payment("lnbc-invoice")


@pytest.mark.asyncio
async def test_prepare_bolt11_payment_does_not_spend_user_liabilities() -> None:
    from routstr.core.settings import settings

    wallet = MagicMock()
    wallet.proofs = [MagicMock(amount=500)]
    wallet.melt_quote = AsyncMock(
        return_value=MagicMock(amount=100, fee_reserve=2, quote="quote-1")
    )
    wallet.get_fees_for_proofs = Mock(return_value=0)

    async def get_wallet(mint_url: str, unit: str = "sat", **_: object) -> MagicMock:
        if unit == "msat":
            raise ValueError("unit unsupported")
        return wallet

    with (
        patch.object(settings, "cashu_mints", ["https://only.test"]),
        patch.object(settings, "primary_mint", "https://only.test"),
        patch("routstr.wallet.get_wallet", side_effect=get_wallet),
        patch(
            "routstr.wallet.get_proofs_per_mint_and_unit",
            side_effect=lambda wallet, *args, **kwargs: wallet.proofs,
        ),
        patch(
            "routstr.wallet.slow_filter_spend_proofs",
            side_effect=lambda proofs, wallet: proofs,
        ),
        patch(
            "routstr.wallet._owner_balance_for_mint_and_unit",
            AsyncMock(return_value=90),
        ),
        pytest.raises(ValueError, match="user liabilities"),
    ):
        await prepare_bolt11_payment("lnbc-invoice")


@pytest.mark.asyncio
async def test_prepare_bolt11_payment_rounds_user_liability_up_to_whole_sats() -> None:
    from routstr.core.settings import settings

    wallet = MagicMock()
    wallet.proofs = [MagicMock(amount=100)]
    wallet.melt_quote = AsyncMock(
        return_value=MagicMock(amount=1, fee_reserve=0, quote="quote-1")
    )
    wallet.get_fees_for_proofs = Mock(return_value=0)

    async def get_wallet(mint_url: str, unit: str = "sat", **_: object) -> MagicMock:
        if unit == "msat":
            raise ValueError("unit unsupported")
        return wallet

    with (
        patch.object(settings, "cashu_mints", ["https://only.test"]),
        patch.object(settings, "primary_mint", "https://only.test"),
        patch("routstr.wallet.get_wallet", side_effect=get_wallet),
        patch(
            "routstr.wallet.get_proofs_per_mint_and_unit",
            side_effect=lambda wallet, *args, **kwargs: wallet.proofs,
        ),
        patch(
            "routstr.wallet.slow_filter_spend_proofs",
            side_effect=lambda proofs, wallet: proofs,
        ),
        patch(
            "routstr.wallet.db.total_user_liability",
            AsyncMock(return_value=99_999),
        ),
        pytest.raises(ValueError, match="user liabilities"),
    ):
        await prepare_bolt11_payment("lnbc-invoice")


@pytest.mark.asyncio
async def test_execute_bolt11_payment_rereserves_when_cancelled() -> None:
    plan = MagicMock()
    plan.proofs = [MagicMock(amount=110)]
    plan.quote.amount = 100
    plan.quote.fee_reserve = 10
    plan.quote.quote = "quote-1"
    plan.invoice = "lnbc-invoice"
    plan.mint_url = "https://mint.test"
    plan.wallet.select_to_send = AsyncMock(return_value=(plan.proofs, 0))
    plan.wallet.set_reserved_for_send = AsyncMock()
    plan.wallet.set_reserved_for_melt = AsyncMock()
    plan.wallet.melt = AsyncMock(side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await execute_bolt11_payment(plan)

    plan.wallet.set_reserved_for_melt.assert_awaited_once_with(
        plan.proofs, reserved=True, quote_id="quote-1"
    )


# ---------------------------------------------------------------------------
# Per-mint adaptive guard + _mint_operation factory/retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_balance_proof_check_uses_large_batches_to_avoid_rate_limit() -> None:
    """Balance reads must not turn a few hundred proofs into many mint requests."""
    from routstr.wallet import slow_filter_spend_proofs

    proofs = [Mock() for _ in range(250)]
    states = [Mock(state="UNSPENT") for _ in proofs]
    wallet = Mock()
    wallet.url = "http://mint:3338"
    wallet.check_proof_state = AsyncMock(return_value=Mock(states=states))
    wallet.set_reserved_for_send = AsyncMock()

    result = await slow_filter_spend_proofs(proofs, wallet)

    assert result == proofs
    wallet.check_proof_state.assert_awaited_once_with(proofs)
    wallet.set_reserved_for_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_mint_rate_guard_bounds_concurrency() -> None:
    from routstr.wallet import _MintRateGuard

    guard = _MintRateGuard("http://mint:3338", 2)
    active = 0
    peak = 0

    async def operation() -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1

    await asyncio.gather(*(guard.run(operation) for _ in range(5)))

    assert peak == 2


@pytest.mark.asyncio
async def test_mint_rate_guard_waits_for_adaptive_cooldown() -> None:
    from routstr.wallet import _MintRateGuard

    guard = _MintRateGuard("http://mint:3338", 2)
    guard._cooldown_until = 15.0
    operation = AsyncMock(return_value="ok")

    with patch("routstr.mint.time.monotonic", return_value=10.0):
        with patch("routstr.mint.asyncio.sleep", AsyncMock()) as sleep:
            assert await guard.run(operation) == "ok"

    sleep.assert_awaited_once_with(5.0)
    operation.assert_awaited_once()


@pytest.mark.asyncio
async def test_mint_rate_guard_exponentially_backs_off_repeated_429s() -> None:
    from routstr.wallet import _MintRateGuard

    guard = _MintRateGuard("http://mint:3338", 4)
    expected_delays = [60, 120, 240, 480, 960, 1920, 3840, 7680, 15360, 25200]
    now = 0.0

    with patch("routstr.mint.time.monotonic") as monotonic:
        for index, expected in enumerate(expected_delays, start=1):
            monotonic.return_value = now
            assert guard.apply_rate_limit_cooldown(60) == expected
            assert guard._consecutive_rate_limits == index
            if index == 1:
                # Concurrent responses from the same 429 wave do not escalate
                # the retry count before the first cooldown probe.
                assert guard.apply_rate_limit_cooldown(60) == expected
                assert guard._consecutive_rate_limits == 1
            now += expected + 1

        monotonic.return_value = now
        operation = AsyncMock(return_value="ok")
        assert await guard.run(operation) == "ok"
        assert guard._consecutive_rate_limits == 0
        assert guard.apply_rate_limit_cooldown(60) == 60


@pytest.mark.asyncio
async def test_mint_rate_guard_allows_one_probe_after_cooldown() -> None:
    from routstr.wallet import _MintRateGuard

    guard = _MintRateGuard("http://mint:3338", 4)
    guard.apply_cooldown(0)
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()
    calls = 0

    async def operation() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            probe_started.set()
            await release_probe.wait()
        return calls

    tasks = [asyncio.create_task(guard.run(operation)) for _ in range(5)]
    await probe_started.wait()
    await asyncio.sleep(0)
    assert calls == 1

    release_probe.set()
    await asyncio.gather(*tasks)
    assert calls == 5
    assert guard._needs_probe is False


def test_mint_rate_guard_rebuilds_when_setting_changes() -> None:
    from routstr.core.settings import settings
    from routstr.wallet import _MintRateGuard

    with patch.object(settings, "mint_max_concurrency", 4):
        first = _MintRateGuard.get("http://mint:3338")
    with patch.object(settings, "mint_max_concurrency", 2):
        second = _MintRateGuard.get("http://mint:3338")

    assert first is not None
    assert second is not None
    assert first is not second
    assert second._max_concurrency == 2


@pytest.mark.asyncio
async def test_mint_rate_guard_keeps_cooldown_when_concurrency_is_unlimited() -> None:
    from routstr.core.settings import settings
    from routstr.wallet import _MintRateGuard

    operation = AsyncMock(return_value="ok")
    with (
        patch.object(settings, "mint_max_concurrency", 0),
        patch("routstr.mint.time.monotonic", return_value=0),
        patch("routstr.mint.asyncio.sleep", AsyncMock()) as sleep,
    ):
        guard = _MintRateGuard.get("http://mint:3338")
        guard.apply_cooldown(5)
        assert await guard.run(operation) == "ok"

    sleep.assert_awaited_once_with(5)
    operation.assert_awaited_once()


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
            with patch.object(settings, "mint_max_concurrency", 1):
                with patch("routstr.mint.time.monotonic", return_value=0.1):
                    with patch("routstr.mint.asyncio.sleep", sleep):
                        result = await _mint_operation(
                            factory, mint_url="http://mint:3338"
                        )

    assert result == "ok"
    sleep.assert_awaited_once_with(60.0)


@pytest.mark.asyncio
async def test_mint_operation_timeout_excludes_adaptive_cooldown() -> None:
    from routstr.core.settings import settings
    from routstr.wallet import _mint_operation, _MintRateGuard

    operation = AsyncMock(return_value="ok")
    with (
        patch.object(settings, "mint_max_concurrency", 1),
        patch.object(settings, "mint_operation_timeout_seconds", 0.01),
        patch("routstr.mint.asyncio.sleep", AsyncMock()) as sleep,
    ):
        guard = _MintRateGuard.get("http://mint:3338")
        guard.apply_cooldown(60)
        assert await _mint_operation(operation, mint_url="http://mint:3338") == "ok"

    sleep.assert_awaited_once()
    operation.assert_awaited_once()


@pytest.mark.asyncio
async def test_default_timeout_allows_retry_after_rate_limit_cooldown() -> None:
    from routstr.core.settings import settings
    from routstr.wallet import _mint_operation

    request = httpx.Request("POST", "http://mint:3338/v1/mint/quote/bolt11")
    response = httpx.Response(429, request=request)
    operation = AsyncMock(
        side_effect=[
            httpx.HTTPStatusError("rate limited", request=request, response=response),
            "ok",
        ]
    )
    with (
        patch.object(settings, "mint_retry_max_attempts", 3),
        patch.object(settings, "mint_operation_timeout_seconds", 30),
        patch.object(settings, "mint_max_concurrency", 1),
        patch("routstr.mint.asyncio.sleep", AsyncMock()),
    ):
        assert await _mint_operation(operation, mint_url="http://mint:3338") == "ok"

    assert operation.await_count == 2


@pytest.mark.asyncio
async def test_mint_operation_retries_httpx_timeout_only_when_safe() -> None:
    from routstr.core.settings import settings
    from routstr.wallet import _mint_operation

    retrying = AsyncMock(side_effect=[httpx.ReadTimeout("slow"), "ok"])
    non_retrying = AsyncMock(side_effect=httpx.ReadTimeout("ambiguous"))

    with patch.object(settings, "mint_retry_max_attempts", 2):
        with patch.object(settings, "mint_operation_timeout_seconds", 0):
            with patch("routstr.mint.asyncio.sleep", AsyncMock()):
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
        # A fresh wallet must load even when the host has been up for less than
        # the reload interval.
        with patch("routstr.mint.time.monotonic", return_value=10.0):
            first, second = await asyncio.gather(
                get_wallet("http://mint:3338"), get_wallet("http://mint:3338")
            )

    assert first is second is mock_wallet
    create.assert_awaited_once()
    mock_wallet.load_mint.assert_awaited_once()
    mock_wallet.load_proofs.assert_awaited_once_with(reload=True)


@pytest.mark.asyncio
async def test_get_wallet_can_surface_429_without_retrying() -> None:
    from routstr.core.settings import settings
    from routstr.wallet import get_wallet

    request = httpx.Request("GET", "http://mint:3338/v1/info")
    response = httpx.Response(429, request=request, headers={"Retry-After": "60"})
    wallet = Mock(
        load_mint=AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "rate limited", request=request, response=response
            )
        ),
        load_proofs=AsyncMock(),
    )

    with (
        patch("routstr.wallet.Wallet.with_db", AsyncMock(return_value=wallet)),
        patch.object(settings, "mint_retry_max_attempts", 3),
        patch.object(settings, "mint_operation_timeout_seconds", 0),
        patch("routstr.mint.asyncio.sleep", AsyncMock()) as sleep,
    ):
        with pytest.raises(httpx.HTTPStatusError):
            await get_wallet("http://mint:3338", retry_on_rate_limit=False)

    wallet.load_mint.assert_awaited_once()
    wallet.load_proofs.assert_not_awaited()
    sleep.assert_not_awaited()


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
            with patch.object(settings, "mint_max_concurrency", 0):
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
            with patch.object(settings, "mint_max_concurrency", 0):
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
            with patch.object(settings, "mint_max_concurrency", 0):
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


def test_raise_on_error_request_identifies_cashu_mint_error() -> None:
    from routstr.mint import MintError
    from routstr.wallet import Wallet

    response = httpx.Response(
        400,
        request=httpx.Request("POST", "https://mint.example/v1/swap"),
        json={"detail": "Token already spent.", "code": 11001},
    )

    with pytest.raises(MintError) as captured:
        Wallet.raise_on_error_request(response)

    assert captured.value.detail == "Token already spent."
    assert captured.value.code == 11001
    assert str(captured.value) == "Mint Error: Token already spent. (Code: 11001)"


@pytest.mark.asyncio
async def test_lightning_mint_fallback_on_cashu_json_429() -> None:
    """The real Cashu JSON-error adapter preserves 429 for fallback."""
    from routstr.core.settings import settings
    from routstr.lightning import _request_mint_with_fallback
    from routstr.wallet import MintRateLimitedError, Wallet

    primary = "http://primary:3338"
    secondary = "http://secondary:3338"

    request = httpx.Request("POST", f"{primary}/v1/mint/quote/bolt11")
    response = httpx.Response(
        429,
        request=request,
        json={"detail": "too many requests", "code": 0},
    )
    with pytest.raises(MintRateLimitedError) as captured:
        Wallet.raise_on_error_request(response)

    mock_primary_wallet = Mock()
    mock_primary_wallet.request_mint = AsyncMock(side_effect=captured.value)

    mock_quote = Mock(request="lnbc1secondary", quote="quote_secondary")
    mock_secondary_wallet = Mock()
    mock_secondary_wallet.request_mint = AsyncMock(return_value=mock_quote)

    wallets_map = {primary: mock_primary_wallet, secondary: mock_secondary_wallet}
    mock_get = AsyncMock(side_effect=lambda m, *a, **kw: wallets_map[m])

    with patch.object(settings, "primary_mint", primary):
        with patch.object(settings, "cashu_mints", [primary, secondary]):
            with patch.object(settings, "mint_retry_max_attempts", 0):
                with patch.object(settings, "mint_max_concurrency", 0):
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
                with patch.object(settings, "mint_max_concurrency", 0):
                    with patch.object(settings, "mint_operation_timeout_seconds", 0):
                        with patch(
                            "routstr.lightning.get_wallet", side_effect=mock_get
                        ):
                            with pytest.raises(MintConnectionError):
                                await _request_mint_with_fallback(1000)


@pytest.mark.asyncio
async def test_lightning_mint_fallback_rejects_zero_amount() -> None:
    """Zero or negative amounts must be rejected before reaching the mint."""
    from routstr.lightning import _request_mint_with_fallback

    with pytest.raises(ValueError, match="amount_sats must be > 0"):
        await _request_mint_with_fallback(0)

    with pytest.raises(ValueError, match="amount_sats must be > 0"):
        await _request_mint_with_fallback(-5)


@pytest.mark.asyncio
async def test_lightning_fallback_on_429_no_in_place_retry() -> None:
    """Same as above but for the lightning.py _request_mint_with_fallback."""
    from routstr.core.settings import settings
    from routstr.lightning import _request_mint_with_fallback

    primary = "http://primary:3338"
    secondary = "http://secondary:3338"

    request = httpx.Request("POST", "http://primary:3338/v1/mint/quote/bolt11")
    response = httpx.Response(429, request=request, headers={"Retry-After": "60"})
    primary_call_count = 0

    async def primary_request_mint(_amount: int) -> None:
        nonlocal primary_call_count
        primary_call_count += 1
        raise httpx.HTTPStatusError("rate limited", request=request, response=response)

    mock_primary_wallet = Mock()
    mock_primary_wallet.request_mint = AsyncMock(side_effect=primary_request_mint)

    mock_quote = Mock(quote="q_secondary", request="lnbc1secondary")
    mock_secondary_wallet = Mock()
    mock_secondary_wallet.request_mint = AsyncMock(return_value=mock_quote)

    wallets_map = {primary: mock_primary_wallet, secondary: mock_secondary_wallet}
    mock_get = AsyncMock(side_effect=lambda m, *a, **kw: wallets_map[m])

    with patch.object(settings, "primary_mint", primary):
        with patch.object(settings, "cashu_mints", [primary, secondary]):
            with patch.object(settings, "mint_retry_max_attempts", 3):
                with patch.object(settings, "mint_max_concurrency", 0):
                    with patch.object(settings, "mint_operation_timeout_seconds", 0):
                        with patch("asyncio.sleep", AsyncMock()) as mock_sleep:
                            with patch(
                                "routstr.lightning.get_wallet", side_effect=mock_get
                            ):
                                _, _, first_mint = await _request_mint_with_fallback(
                                    1000
                                )
                                _, _, second_mint = await _request_mint_with_fallback(
                                    1000
                                )

    assert first_mint == second_mint == secondary
    assert primary_call_count == 1
    assert mock_secondary_wallet.request_mint.await_count == 2
    mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# _is_mint_rate_limited — strict HTTP 429 only (no substring matching)
# ---------------------------------------------------------------------------


def _http_429_error(message: str = "") -> httpx.HTTPStatusError:
    """Create an HTTP 429 error with optional message in the response body."""
    body = json.dumps({"error": message}) if message else "{}"
    return httpx.HTTPStatusError(
        message or "Too Many Requests",
        request=httpx.Request("POST", "http://m"),
        response=httpx.Response(429, content=body.encode()),
    )


def _http_500_error(message: str = "") -> httpx.HTTPStatusError:
    """Create an HTTP 500 error with optional message in the response body."""
    body = json.dumps({"error": message}) if message else "{}"
    return httpx.HTTPStatusError(
        message or "Internal Server Error",
        request=httpx.Request("POST", "http://m"),
        response=httpx.Response(500, content=body.encode()),
    )


@pytest.mark.parametrize(
    "error,expected",
    [
        # True: HTTP 429 is always a rate limit, regardless of message.
        (_http_429_error(""), True),
        (_http_429_error("Too Many Requests"), True),
        (_http_429_error("completely unrelated message"), True),
        # False: HTTP 500 is NOT a rate limit, even if the message says "rate limit".
        (_http_500_error(""), False),
        (_http_500_error("rate limit exceeded"), False),
        (_http_500_error("too many requests"), False),
        # False: non-HTTP errors with "rate limit" in message.
        (ValueError("rate limit exceeded"), False),
        (ValueError("too many requests try again"), False),
        (RuntimeError("internal rate limit hit"), False),
        # False: generic transport errors.
        (httpx.ConnectError("connection refused"), False),
        (httpx.ReadTimeout("timed out"), False),
        (MintConnectionError("mint down"), False),
        # Wrapped: HTTP 429 in the cause chain IS detected.
        (_chain(ValueError("wrapped"), _http_429_error()), True),
        # Wrapped: HTTP 500 with "rate limit" text in cause is NOT detected.
        (
            _chain(ValueError("wrapped"), _http_500_error("rate limit exceeded")),
            False,
        ),
    ],
)
def test_is_mint_rate_limited_strictness(error: BaseException, expected: bool) -> None:
    assert _is_mint_rate_limited(error) is expected


def test_is_mint_rate_limited_survives_cycle() -> None:
    """A pathological cause/context cycle must not hang the classifier."""
    a = ValueError("a")
    b = _http_429_error()
    a.__cause__ = b
    b.__context__ = a
    assert _is_mint_rate_limited(a) is True


# ---------------------------------------------------------------------------
# classify_redemption_error — mint_rate_limited vs mint_unreachable
# ---------------------------------------------------------------------------


def test_classify_rate_limit_returns_mint_rate_limited() -> None:
    """HTTP 429 from a mint is classified as mint_rate_limited, not
    mint_unreachable, so callers can distinguish temporary back-off from
    permanent mint outages."""
    classified = classify_redemption_error(_http_429_error("Too Many Requests"))
    assert classified is not None
    type_, status, _msg, code = classified
    assert type_ == "mint_rate_limited"
    assert status == 503
    assert code == "cashu_mint_rate_limited"


def test_classify_rate_limit_takes_priority_over_connection_error() -> None:
    """When a 429 is wrapped in a chain that also contains a transport error,
    mint_rate_limited wins because it is checked first."""
    inner = _http_429_error()
    outer = MintConnectionError("outer")
    outer.__cause__ = inner

    classified = classify_redemption_error(outer)
    assert classified is not None
    type_, status, _msg, code = classified
    assert type_ == "mint_rate_limited"
    assert code == "cashu_mint_rate_limited"


def test_classify_connection_error_still_returns_mint_unreachable() -> None:
    """Transport failures without a 429 in the chain are still
    classified as mint_unreachable."""
    classified = classify_redemption_error(httpx.ConnectError("connection refused"))
    assert classified is not None
    type_, status, _msg, code = classified
    assert type_ == "mint_unreachable"
    assert status == 503
    assert code == "cashu_mint_unreachable"


def test_classify_500_with_rate_limit_text_is_not_mint_rate_limited() -> None:
    """An HTTP 500 whose body happens to mention 'rate limit' is NOT
    classified as mint_rate_limited — it falls through to the generic
    error handler."""
    classified = classify_redemption_error(
        _http_500_error("database rate limit exceeded")
    )
    # Should NOT be mint_rate_limited or mint_unreachable.
    if classified is not None:
        type_, _status, _msg, code = classified
        assert type_ != "mint_rate_limited"
        assert code != "cashu_mint_rate_limited"


# ---------------------------------------------------------------------------
# _MintRateGuard — probe backoff escalation and recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_escalates_consecutive_rate_limits() -> None:
    from routstr.mint import MintRateGuard

    guard = MintRateGuard("http://mint", max_concurrency=0)
    guard.apply_rate_limit_cooldown()
    guard._cooldown_until = 0.0

    with pytest.raises(httpx.HTTPStatusError):
        await guard.run(AsyncMock(side_effect=_http_429_error()))

    assert guard._consecutive_rate_limits == 2
    assert guard._needs_probe is True
    assert guard.cooldown_remaining() > 60


@pytest.mark.asyncio
async def test_probe_recovery_resets_consecutive_rate_limits() -> None:
    from routstr.mint import MintRateGuard

    guard = MintRateGuard("http://mint", max_concurrency=0)
    guard.apply_rate_limit_cooldown()
    guard._cooldown_until = 0.0

    assert await guard.run(AsyncMock(return_value="ok")) == "ok"

    assert guard._consecutive_rate_limits == 0
    assert guard._needs_probe is False
    assert guard.cooldown_remaining() == 0.0


async def test_payout_reloads_wallet_snapshot_under_guard() -> None:
    """Payout must not trust a cached proof snapshot from before the guard."""
    from routstr.wallet import _payout_mint_and_unit

    mock_get_wallet = AsyncMock(side_effect=RuntimeError("stop after get_wallet"))
    with patch("routstr.wallet.get_wallet", mock_get_wallet):
        await _payout_mint_and_unit("https://mint.example.com", "sat")

    mock_get_wallet.assert_awaited_once_with(
        "https://mint.example.com", "sat", force_reload=True
    )


@pytest.mark.asyncio
async def test_load_mint_propagates_rate_limit() -> None:
    from routstr.mint import MintRateLimitedError
    from routstr.wallet import Wallet

    wallet = Wallet.__new__(Wallet)
    wallet.url = "https://rate-limited-mint.example"
    error = MintRateLimitedError(
        "Cashu mint rate limited",
        request=httpx.Request("GET", "https://mint.example/v1/keysets"),
        response=httpx.Response(429),
    )
    with (
        patch.object(wallet, "load_mint_keysets", new=AsyncMock(side_effect=error)),
        pytest.raises(MintRateLimitedError),
    ):
        await wallet.load_mint()


@pytest.mark.asyncio
async def test_load_mint_propagates_connection_error() -> None:
    from routstr.wallet import Wallet

    wallet = Wallet.__new__(Wallet)
    wallet.url = "https://unavailable-mint.example"
    error = httpx.ConnectError("mint unavailable")
    with (
        patch.object(wallet, "load_mint_keysets", new=AsyncMock(side_effect=error)),
        pytest.raises(httpx.ConnectError) as captured,
    ):
        await wallet.load_mint()

    assert captured.value is error


@pytest.mark.asyncio
async def test_load_mint_runs_keysets_activation_and_info() -> None:
    from routstr.wallet import Wallet

    wallet = Wallet.__new__(Wallet)
    wallet.url = "https://mint-load.example"
    with (
        patch.object(wallet, "load_mint_keysets", new=AsyncMock()) as load_keysets,
        patch.object(wallet, "activate_keyset", new=AsyncMock()) as activate,
        patch.object(wallet, "load_mint_info", new=AsyncMock()) as load_info,
    ):
        await wallet.load_mint(keyset_id="abc")

    load_keysets.assert_awaited_once_with(False)
    activate.assert_awaited_once_with("abc")
    load_info.assert_awaited_once_with(reload=True)
