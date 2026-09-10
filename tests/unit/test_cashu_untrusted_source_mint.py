"""Tokens issued by an untrusted mint are rejected before any mint contact.

A client-supplied Cashu token names its own mint. Every redemption path used
to load that mint's keysets under ``wallet_operation_guard`` with the full
timeout-retry window, so a silent mint could hold the shared wallet lock for
minutes per request from unauthenticated endpoints. Now the mint must be
``primary_mint`` or one of ``cashu_mints``; anything else fails offline with a
dedicated error type and code.
"""

from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from typing import AsyncGenerator, Iterator, cast
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.auth import validate_bearer_key
from routstr.core.settings import settings
from routstr.mint import MintCooldownError
from routstr.payment.helpers import check_token_balance
from routstr.wallet import (
    SourceMintConnectionError,
    TokenConsumedError,
    UntrustedSourceMintError,
    classify_redemption_error,
    is_mint_timeout,
    is_trusted_source_mint,
    recieve_token,
    resolve_trusted_source_mint,
)

PRIMARY = "http://primary:3338"
SECONDARY = "http://secondary:3338"
UNTRUSTED = "http://evil:3338"


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    db_session = AsyncSession(engine, expire_on_commit=False)
    try:
        yield db_session
    finally:
        await db_session.close()
        await engine.dispose()


@contextmanager
def _trusted_mints() -> Iterator[None]:
    with ExitStack() as stack:
        stack.enter_context(patch.object(settings, "primary_mint", PRIMARY))
        stack.enter_context(patch.object(settings, "cashu_mints", [SECONDARY]))
        yield


def _token(mint: str) -> SimpleNamespace:
    return SimpleNamespace(mint=mint, unit="sat", amount=100, keysets=["k"])


def test_is_trusted_source_mint() -> None:
    with _trusted_mints():
        assert is_trusted_source_mint(PRIMARY)
        assert is_trusted_source_mint(SECONDARY)
        assert not is_trusted_source_mint(UNTRUSTED)


@pytest.mark.parametrize(
    "configured,token_mint",
    [
        ("https://mint.example", "https://mint.example/"),
        ("https://mint.example/", "https://mint.example"),
        ("https://mint.example", "https://mint.example///"),
        ("https://mint.example", "HTTPS://MINT.EXAMPLE"),
        ("HTTPS://Mint.Example", "https://mint.example"),
        ("https://mint.example", "https://mint.example:443"),
        ("https://mint.example:443", "https://mint.example"),
        ("http://mint.example", "http://mint.example:80"),
        ("  https://mint.example/  ", "https://mint.example"),
        ("https://mint.example/Bitcoin", "https://mint.example/Bitcoin/"),
    ],
)
def test_trusted_mint_matching_ignores_cosmetic_url_differences(
    configured: str, token_mint: str
) -> None:
    with patch.object(settings, "primary_mint", configured):
        with patch.object(settings, "cashu_mints", []):
            assert is_trusted_source_mint(token_mint)


@pytest.mark.parametrize(
    "token_mint",
    [
        "https://mint.example@evil.example",
        "https://mint.example:pw@evil.example",
        "https://mint.example.evil.example",
        "https://evil.example/?x=https://mint.example",
        "https://evil.example#https://mint.example",
        "https://mint.example:8443",
        "http://mint.example",
        "https://mint.example.",
        "https://mint.example/bitcoin",
        "https://evil.example",
    ],
)
def test_trusted_mint_matching_never_folds_onto_another_host(token_mint: str) -> None:
    """Normalization must not become an accept-bypass: only cosmetic spelling
    differences may fold, never anything that can resolve somewhere else."""
    with patch.object(settings, "primary_mint", "https://mint.example/Bitcoin"):
        with patch.object(settings, "cashu_mints", ["https://mint.example"]):
            assert not is_trusted_source_mint(token_mint)


@pytest.mark.parametrize(
    "token_mint",
    [
        "https://mint.exa\tmple",
        "https://mint.exa\nmple",
        "https://mint.exa\rmple",
    ],
)
def test_trusted_mint_matching_rejects_embedded_control_characters(
    token_mint: str,
) -> None:
    """``urlsplit`` deletes tab/CR/LF before parsing, so such a URL would be
    checked as one string and dialled as another."""
    with patch.object(settings, "primary_mint", "https://mint.example"):
        with patch.object(settings, "cashu_mints", []):
            assert not is_trusted_source_mint(token_mint)


@pytest.mark.parametrize(
    "token_mint",
    ["", "   ", "https://", "://mint.example", "mint.example"],
)
def test_trusted_mint_matching_rejects_degenerate_urls(token_mint: str) -> None:
    with patch.object(settings, "primary_mint", "https://mint.example"):
        with patch.object(settings, "cashu_mints", []):
            assert not is_trusted_source_mint(token_mint)


def test_unset_primary_mint_never_makes_a_token_trusted() -> None:
    """An unset primary mint must not turn an empty token mint into a match."""
    with patch.object(settings, "primary_mint", ""):
        with patch.object(settings, "cashu_mints", []):
            assert not is_trusted_source_mint("")
            assert not is_trusted_source_mint("https://evil.example")


def test_trusted_mint_matching_keeps_path_case_sensitive() -> None:
    with patch.object(settings, "primary_mint", "https://mint.minibits.cash/Bitcoin"):
        with patch.object(settings, "cashu_mints", []):
            assert is_trusted_source_mint("https://mint.minibits.cash/Bitcoin/")
            assert not is_trusted_source_mint("https://mint.minibits.cash/bitcoin")


def test_trusted_mint_matching_rejects_unparseable_port() -> None:
    with patch.object(settings, "primary_mint", "https://mint.example"):
        with patch.object(settings, "cashu_mints", []):
            assert not is_trusted_source_mint("https://mint.example:notaport")


def test_trusted_mint_matching_rejects_malformed_url() -> None:
    with patch.object(settings, "primary_mint", "https://mint.example"):
        with patch.object(settings, "cashu_mints", []):
            assert not is_trusted_source_mint("https://[::1/Bitcoin")
            assert resolve_trusted_source_mint("https://[::1/Bitcoin") is None


def test_resolve_returns_operator_spelling() -> None:
    configured = "https://mint.example/Bitcoin"
    with patch.object(settings, "primary_mint", configured):
        with patch.object(settings, "cashu_mints", []):
            assert (
                resolve_trusted_source_mint("HTTPS://MINT.EXAMPLE:443/Bitcoin///")
                == configured
            )


@pytest.mark.asyncio
async def test_recieve_token_uses_canonical_mint_url() -> None:
    variant = PRIMARY.upper() + "///"
    get_wallet = AsyncMock(return_value=object())
    redeem = AsyncMock(return_value=(90, "sat", variant))
    with (
        _trusted_mints(),
        patch(
            "routstr.wallet.deserialize_token_from_string",
            return_value=_token(variant),
        ),
        patch("routstr.wallet.get_wallet", get_wallet),
        patch("routstr.wallet._redeem_same_mint", redeem),
    ):
        amount, unit, mint_url = await recieve_token("cashuAvariant")

    assert (amount, unit, mint_url) == (90, "sat", PRIMARY)
    get_wallet.assert_awaited_once_with(PRIMARY, "sat", load=False)


def test_classification_has_dedicated_type_and_code() -> None:
    classified = classify_redemption_error(UntrustedSourceMintError("x"))
    assert classified == (
        "untrusted_mint",
        400,
        "Cashu token was issued by a mint this node does not accept",
        "cashu_untrusted_source_mint",
    )


@pytest.mark.asyncio
async def test_recieve_token_rejects_untrusted_mint_before_mint_contact() -> None:
    """The gate runs inside the wallet lock but before ``get_wallet``, so an
    untrusted token never reaches the mint over the network."""
    get_wallet = AsyncMock()
    with (
        _trusted_mints(),
        patch(
            "routstr.wallet.deserialize_token_from_string",
            return_value=_token(UNTRUSTED),
        ),
        patch("routstr.wallet.get_wallet", get_wallet),
    ):
        with pytest.raises(UntrustedSourceMintError):
            await recieve_token("cashuAuntrusted")

    get_wallet.assert_not_awaited()


@pytest.mark.asyncio
async def test_bearer_untrusted_mint_returns_400_with_dedicated_code(
    session: AsyncSession,
) -> None:
    get_wallet = AsyncMock()
    with (
        _trusted_mints(),
        patch(
            "routstr.auth.deserialize_token_from_string",
            return_value=_token(UNTRUSTED),
        ),
        patch(
            "routstr.wallet.deserialize_token_from_string",
            return_value=_token(UNTRUSTED),
        ),
        patch("routstr.wallet.get_wallet", get_wallet),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await validate_bearer_key("cashuAuntrusted", session)

    assert exc_info.value.status_code == 400
    detail = cast(dict[str, dict[str, str]], exc_info.value.detail)
    assert detail["error"]["type"] == "untrusted_mint"
    assert detail["error"]["code"] == "cashu_untrusted_source_mint"
    get_wallet.assert_not_awaited()


def test_check_token_balance_rejects_untrusted_mint() -> None:
    with (
        _trusted_mints(),
        patch(
            "routstr.payment.helpers.deserialize_token_from_string",
            return_value=_token(UNTRUSTED),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            check_token_balance({"x-cashu": "cashuAuntrusted"}, {"model": "m"}, 1)

    assert exc_info.value.status_code == 400
    detail = cast(dict[str, dict[str, str]], exc_info.value.detail)
    assert detail["error"]["type"] == "untrusted_mint"
    assert detail["error"]["code"] == "cashu_untrusted_source_mint"


def test_check_token_balance_accepts_trusted_mints() -> None:
    for mint in (PRIMARY, SECONDARY):
        with (
            _trusted_mints(),
            patch(
                "routstr.payment.helpers.deserialize_token_from_string",
                return_value=_token(mint),
            ),
        ):
            check_token_balance({"x-cashu": "cashuAtrusted"}, {"model": "m"}, 1)


def _http_429(retry_after: str | None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://primary:3338/v1/swap")
    headers = {"Retry-After": retry_after} if retry_after else {}
    response = httpx.Response(429, request=request, headers=headers)
    return httpx.HTTPStatusError("rate limited", request=request, response=response)


@pytest.mark.parametrize(
    "error",
    [_http_429("42"), _http_429(None), MintCooldownError(PRIMARY, 12.4)],
)
def test_rate_limit_asks_to_retry_later(error: Exception) -> None:
    assert classify_redemption_error(error) == (
        "mint_rate_limited",
        503,
        "Cashu mint is rate-limiting requests; retry later",
        "cashu_mint_rate_limited",
    )


def test_timeout_has_its_own_code() -> None:
    wrapped = SourceMintConnectionError("Issuing Cashu mint is unreachable")
    wrapped.__cause__ = httpx.ReadTimeout("read timed out")
    assert classify_redemption_error(wrapped) == (
        "mint_timeout",
        503,
        "Cashu mint did not respond in time; retry later",
        "cashu_mint_timeout",
    )


def test_source_mint_unreachable_asks_to_retry() -> None:
    wrapped = SourceMintConnectionError("Issuing Cashu mint is unreachable")
    wrapped.__cause__ = httpx.ConnectError("refused")
    assert classify_redemption_error(wrapped) == (
        "mint_unreachable",
        503,
        "The mint that issued this Cashu token is unreachable; retry later",
        "cashu_source_mint_unreachable",
    )


def test_timeout_wrapped_in_consumed_token_is_not_retryable() -> None:
    consumed = TokenConsumedError("credit failed after melt")
    consumed.__cause__ = httpx.ReadTimeout("read timed out")
    classified = classify_redemption_error(consumed)
    assert classified is not None
    assert classified[3] == "cashu_token_consumed"
    assert not is_mint_timeout(consumed)
