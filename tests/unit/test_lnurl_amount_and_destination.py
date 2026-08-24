"""LNURL payments must verify the invoice amount and the destination.

Findings 4 and 5 ship together: the amount check is what makes it safe to
hand an LNURL a set of unreserved proofs, and reserving only after that check
is what stops a pre-dispatch failure from stranding proofs.
"""

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cashu.core.base import MeltQuoteState

from routstr.payment import lnurl as lnurl_module
from routstr.payment.lnurl import (
    LNURLError,
    get_lnurl_data,
    get_lnurl_invoice,
    raw_send_to_lnurl,
)

LNURL_DATA = {
    "callback_url": "https://ln.tld/cb",
    "min_sendable": 1_000,
    "max_sendable": 100_000_000,
}

# 1000 sat minus the 11 sat estimated fee reserve.
EXPECTED_QUOTE_SAT = 989


def _wallet(
    quote_amount: int = EXPECTED_QUOTE_SAT,
) -> tuple[MagicMock, list[MagicMock]]:
    proofs = [MagicMock(amount=1000)]
    wallet = MagicMock(url="https://mint.test")
    wallet.melt_quote = AsyncMock(
        return_value=MagicMock(fee_reserve=1, quote="q", amount=quote_amount)
    )
    wallet.select_to_send = AsyncMock(return_value=(proofs, None))
    wallet.melt = AsyncMock(return_value=MagicMock(state=MeltQuoteState.paid))
    wallet.set_reserved_for_send = AsyncMock()
    return wallet, proofs


def _lnurl_patches() -> tuple[Any, Any]:
    return (
        patch(
            "routstr.payment.lnurl.get_lnurl_data",
            AsyncMock(return_value=LNURL_DATA),
        ),
        patch(
            "routstr.payment.lnurl.get_lnurl_invoice",
            AsyncMock(return_value=("lnbc1...", {})),
        ),
    )


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> Any:
    real_client = httpx.AsyncClient

    def factory(*_args: object, **_kwargs: object) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler))

    return patch.object(lnurl_module.httpx, "AsyncClient", factory)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "quote_amount", [EXPECTED_QUOTE_SAT + 1, EXPECTED_QUOTE_SAT * 5]
)
async def test_raw_send_to_lnurl_rejects_oversized_invoice(quote_amount: int) -> None:
    wallet, proofs = _wallet(quote_amount)
    data_patch, invoice_patch = _lnurl_patches()

    with data_patch, invoice_patch, pytest.raises(LNURLError, match="invoice amount"):
        await raw_send_to_lnurl(wallet, proofs, "owner@ln.tld", "sat", amount=1000)

    wallet.select_to_send.assert_not_awaited()
    wallet.melt.assert_not_awaited()


@pytest.mark.asyncio
async def test_raw_send_to_lnurl_rejects_undersized_invoice() -> None:
    wallet, proofs = _wallet(EXPECTED_QUOTE_SAT - 1)
    data_patch, invoice_patch = _lnurl_patches()

    with data_patch, invoice_patch, pytest.raises(LNURLError, match="invoice amount"):
        await raw_send_to_lnurl(wallet, proofs, "owner@ln.tld", "sat", amount=1000)

    wallet.select_to_send.assert_not_awaited()
    wallet.melt.assert_not_awaited()


@pytest.mark.asyncio
async def test_raw_send_to_lnurl_rejects_invoice_before_quote_checkpoint() -> None:
    wallet, proofs = _wallet(EXPECTED_QUOTE_SAT * 2)
    checkpoint = AsyncMock()
    data_patch, invoice_patch = _lnurl_patches()

    with data_patch, invoice_patch, pytest.raises(LNURLError):
        await raw_send_to_lnurl(
            wallet,
            proofs,
            "owner@ln.tld",
            "sat",
            amount=1000,
            on_melt_quote=checkpoint,
        )

    checkpoint.assert_not_awaited()


@pytest.mark.asyncio
async def test_raw_send_to_lnurl_accepts_exact_invoice() -> None:
    wallet, proofs = _wallet()
    data_patch, invoice_patch = _lnurl_patches()

    with data_patch, invoice_patch:
        paid = await raw_send_to_lnurl(
            wallet, proofs, "owner@ln.tld", "sat", amount=1000
        )

    assert paid == EXPECTED_QUOTE_SAT * 1000
    wallet.select_to_send.assert_awaited_once()
    wallet.melt.assert_awaited_once()


@pytest.mark.asyncio
async def test_raw_send_to_lnurl_msat_unit_compares_in_wallet_unit() -> None:
    # 1_000_000 msat minus an 11 sat fee reserve leaves 989_000 msat.
    wallet, proofs = _wallet(989_000)
    proofs[0].amount = 1_000_000
    wallet.select_to_send = AsyncMock(return_value=(proofs, None))
    data_patch, invoice_patch = _lnurl_patches()

    with data_patch, invoice_patch:
        paid = await raw_send_to_lnurl(
            wallet, proofs, "owner@ln.tld", "msat", amount=1_000_000
        )

    assert paid == 989_000


@pytest.mark.asyncio
async def test_raw_send_to_lnurl_requires_an_explicit_amount() -> None:
    wallet, proofs = _wallet()
    data_patch, invoice_patch = _lnurl_patches()

    with data_patch, invoice_patch, pytest.raises(ValueError, match="amount"):
        await raw_send_to_lnurl(wallet, proofs, "owner@ln.tld", "sat")

    wallet.select_to_send.assert_not_awaited()
    wallet.melt.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    [
        "owner@127.0.0.1",
        "owner@localhost",
        "owner@10.0.0.5",
        "owner@[::1]",
        "http://ln.tld/lnurlp/owner",
    ],
)
async def test_get_lnurl_data_rejects_non_public_destination(address: str) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200, json={"tag": "payRequest", "callback": "https://x/y"}
        )

    with _mock_client(handler), pytest.raises(LNURLError):
        await get_lnurl_data(address)

    assert requested == []


@pytest.mark.asyncio
async def test_get_lnurl_data_rejects_redirect_to_private_host() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ln.tld":
            return httpx.Response(
                302, headers={"location": "https://169.254.169.254/latest/meta-data"}
            )
        return httpx.Response(
            200, json={"tag": "payRequest", "callback": "https://x/y"}
        )

    with _mock_client(handler), pytest.raises(LNURLError, match="destination"):
        await get_lnurl_data("owner@ln.tld")


@pytest.mark.asyncio
async def test_get_lnurl_data_rejects_downgrade_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "https":
            return httpx.Response(302, headers={"location": "http://ln.tld/plain"})
        return httpx.Response(
            200, json={"tag": "payRequest", "callback": "https://x/y"}
        )

    with _mock_client(handler), pytest.raises(LNURLError, match="destination"):
        await get_lnurl_data("owner@ln.tld")


@pytest.mark.asyncio
async def test_get_lnurl_data_rejects_private_callback_url() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"tag": "payRequest", "callback": "http://127.0.0.1:8000/cb"}
        )

    with _mock_client(handler), pytest.raises(LNURLError, match="destination"):
        await get_lnurl_data("owner@ln.tld")


@pytest.mark.asyncio
async def test_get_lnurl_data_error_does_not_leak_response_body() -> None:
    secret = "SUPERSECRETBODYMARKER"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tag": secret, "callback": secret})

    with _mock_client(handler), pytest.raises(LNURLError) as excinfo:
        await get_lnurl_data("owner@ln.tld")

    assert secret not in str(excinfo.value)


@pytest.mark.asyncio
async def test_get_lnurl_invoice_error_does_not_leak_response_body() -> None:
    secret = "SUPERSECRETBODYMARKER"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"reason": secret, "internal": secret})

    with _mock_client(handler), pytest.raises(LNURLError) as excinfo:
        await get_lnurl_invoice("https://ln.tld/cb", 1000)

    assert secret not in str(excinfo.value)


@pytest.mark.asyncio
async def test_get_lnurl_invoice_rejects_redirect_to_private_host() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ln.tld":
            return httpx.Response(302, headers={"location": "https://192.168.1.1/cb"})
        return httpx.Response(200, json={"pr": "lnbc1..."})

    with _mock_client(handler), pytest.raises(LNURLError, match="destination"):
        await get_lnurl_invoice("https://ln.tld/cb", 1000)


@pytest.mark.asyncio
async def test_send_to_lnurl_does_not_reserve_before_lnurl_validation() -> None:
    from routstr import wallet as wallet_module

    wallet, proofs = _wallet()

    with (
        patch.object(
            wallet_module,
            "find_trusted_mint_with_funds",
            AsyncMock(return_value="https://mint.test"),
        ),
        patch.object(wallet_module, "get_wallet", AsyncMock(return_value=wallet)),
        patch.object(
            wallet_module,
            "get_proofs_per_mint_and_unit",
            MagicMock(return_value=proofs),
        ),
        patch.object(
            wallet_module,
            "raw_send_to_lnurl",
            AsyncMock(side_effect=LNURLError("destination rejected")),
        ) as raw_send,
        pytest.raises(LNURLError),
    ):
        await wallet_module.send_to_lnurl(
            1000, "sat", "https://mint.test", "owner@ln.tld"
        )

    wallet.select_to_send.assert_not_awaited()
    assert raw_send.await_args is not None
    assert raw_send.await_args.args[1] is proofs
    assert raw_send.await_args.kwargs["amount"] == 1000
