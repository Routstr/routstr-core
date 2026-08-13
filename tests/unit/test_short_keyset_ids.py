from typing import cast
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from cashu.core.base import (
    MeltQuoteState,
    Proof,
    TokenV4,
    TokenV4Proof,
    TokenV4Token,
)

from routstr.wallet import (
    SourceMintConnectionError,
    Wallet,
    _redeem_same_mint,
    classify_redemption_error,
    swap_to_trusted_mint,
)

MINT_URL = "https://mint.example"
FULL_V2_ID = "01" + "11" * 32
SHORT_V2_ID = FULL_V2_ID[:16]
OTHER_V2_ID = "01" + "22" * 32
LEGACY_V1_ID = "00" + "33" * 7


def _token(keyset_id: str = SHORT_V2_ID, amounts: tuple[int, ...] = (5,)) -> TokenV4:
    return TokenV4(
        m=MINT_URL,
        u="sat",
        t=[
            TokenV4Token(
                i=bytes.fromhex(keyset_id),
                p=[
                    TokenV4Proof(
                        a=amount,
                        s=f"synthetic-proof-{index}",
                        c=bytes.fromhex("02" + f"{index + 1:02x}" * 32),
                    )
                    for index, amount in enumerate(amounts)
                ],
            )
        ],
    )


def _wallet_with_keysets(*keyset_ids: str) -> Mock:
    wallet = Mock(
        keysets={keyset_id: Mock(id=keyset_id) for keyset_id in keyset_ids},
        load_mint_keysets=AsyncMock(),
        split=AsyncMock(),
        get_fees_for_proofs=Mock(return_value=0),
        verify_proofs_dleq=Mock(),
    )

    async def activate_keyset() -> None:
        if not wallet.keysets:
            raise Exception("No active keyset")
        wallet.keyset_id = next(iter(wallet.keysets))

    async def expand_with_cashu(proofs: list) -> None:
        await Wallet._expand_short_keyset_ids(wallet, proofs)

    wallet.activate_keyset = AsyncMock(side_effect=activate_keyset)
    wallet._expand_short_keyset_ids = AsyncMock(side_effect=expand_with_cashu)
    return wallet


@pytest.mark.asyncio
async def test_same_mint_redeem_reuses_the_resolved_proofs() -> None:
    token = _token(amounts=(8, 9))
    wallet = _wallet_with_keysets(FULL_V2_ID)

    assert await _redeem_same_mint(wallet, token) == (17, "sat", MINT_URL)

    split_proofs = wallet.split.await_args.kwargs["proofs"]
    assert [proof.id for proof in split_proofs] == [FULL_V2_ID, FULL_V2_ID]
    assert wallet.keyset_id == FULL_V2_ID
    assert token.proofs[0].id == SHORT_V2_ID
    assert wallet.verify_proofs_dleq.call_args.args[0] is split_proofs
    assert wallet.get_fees_for_proofs.call_args.args[0] is split_proofs
    wallet.load_mint_keysets.assert_awaited_once_with()
    wallet.activate_keyset.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_legacy_v1_id_remains_compatible() -> None:
    token = _token(LEGACY_V1_ID)
    wallet = _wallet_with_keysets(LEGACY_V1_ID)

    assert await _redeem_same_mint(wallet, token) == (5, "sat", MINT_URL)

    split_proofs = wallet.split.await_args.kwargs["proofs"]
    assert split_proofs[0].id == LEGACY_V1_ID
    wallet._expand_short_keyset_ids.assert_awaited_once_with(split_proofs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "known_ids",
    [
        (OTHER_V2_ID,),
        (
            FULL_V2_ID,
            SHORT_V2_ID + "ff" * 25,
        ),
    ],
    ids=["unknown", "ambiguous"],
)
async def test_unknown_or_ambiguous_short_id_fails_before_mutation(
    known_ids: tuple[str, ...],
) -> None:
    wallet = _wallet_with_keysets(*known_ids)

    with pytest.raises(ValueError, match="unknown or ambiguous keyset") as caught:
        await _redeem_same_mint(wallet, _token())

    wallet.split.assert_not_awaited()
    classified = classify_redemption_error(caught.value)
    assert classified is not None
    assert classified[1] == 400
    assert classified[3] == "cashu_token_redemption_failed"


@pytest.mark.asyncio
async def test_missing_mint_keysets_is_retryable_before_mutation() -> None:
    wallet = _wallet_with_keysets()

    with pytest.raises(SourceMintConnectionError) as caught:
        await _redeem_same_mint(wallet, _token())

    wallet.split.assert_not_awaited()
    classified = classify_redemption_error(caught.value)
    assert classified is not None
    assert classified[1] == 503
    assert classified[3] == "cashu_source_mint_unreachable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "load_error",
    [
        httpx.ConnectError("mint unavailable"),
        Exception("Mint Error: restarting"),
    ],
    ids=["transport", "mint-error"],
)
async def test_cached_keysets_do_not_mask_a_refresh_failure(
    load_error: Exception,
) -> None:
    wallet = _wallet_with_keysets(OTHER_V2_ID)
    wallet.load_mint_keysets.side_effect = load_error

    with pytest.raises(SourceMintConnectionError) as caught:
        await _redeem_same_mint(wallet, _token())

    wallet.activate_keyset.assert_not_awaited()
    wallet._expand_short_keyset_ids.assert_not_awaited()
    wallet.split.assert_not_awaited()
    classified = classify_redemption_error(caught.value)
    assert classified is not None
    assert classified[1] == 503
    assert classified[3] == "cashu_source_mint_unreachable"


@pytest.mark.asyncio
async def test_cross_mint_swap_reuses_resolved_proofs_end_to_end() -> None:
    token = _token()
    source_wallet = _wallet_with_keysets(FULL_V2_ID)
    source_wallet.melt_quote = AsyncMock(
        return_value=Mock(quote="melt-quote", amount=5, fee_reserve=0)
    )
    source_wallet.melt = AsyncMock(return_value=Mock(state=MeltQuoteState.paid))

    destination_url = "https://trusted-mint.example"
    destination_wallet = Mock(
        load_proofs=AsyncMock(),
        available_balance=Mock(amount=0),
        mint=AsyncMock(),
    )
    mint_quote = Mock(quote="mint-quote", request="lnbc-test-invoice")
    calculate_amount = AsyncMock(return_value=5)

    with (
        patch("routstr.wallet.settings.primary_mint", destination_url),
        patch("routstr.wallet.settings.primary_mint_unit", "sat"),
        patch("routstr.wallet.settings.cashu_mints", [destination_url]),
        patch(
            "routstr.wallet._calculate_swap_amount",
            calculate_amount,
        ),
        patch(
            "routstr.wallet._request_mint_with_fallback",
            AsyncMock(return_value=(destination_wallet, destination_url, mint_quote)),
        ),
    ):
        assert await swap_to_trusted_mint(token, source_wallet) == (
            5,
            "sat",
            destination_url,
        )

    calculate_call = calculate_amount.await_args
    assert calculate_call is not None
    resolved = cast(list[Proof], calculate_call.args[5])
    assert resolved[0].id == FULL_V2_ID
    assert source_wallet.get_fees_for_proofs.call_args.args[0] is resolved
    assert source_wallet.melt.await_args.kwargs["proofs"] is resolved
