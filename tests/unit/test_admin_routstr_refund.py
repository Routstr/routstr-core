from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

import routstr.upstream.helpers as upstream_helpers
import routstr.wallet as wallet_module
from routstr.core import admin
from routstr.upstream.routstr import RoutstrUpstreamProvider

_RealAsyncClient = httpx.AsyncClient


def _upstream_answering(
    monkeypatch: pytest.MonkeyPatch, status_code: int, body: Any
) -> RoutstrUpstreamProvider:
    """Wire the admin refund endpoint to an upstream node returning ``body``."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/balance/refund"
        return httpx.Response(status_code, json=body)

    return _upstream_handled_by(monkeypatch, handler)


def _upstream_handled_by(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> RoutstrUpstreamProvider:
    """Wire the admin refund endpoint to an upstream node served by ``handler``."""

    def client_factory() -> httpx.AsyncClient:
        return _RealAsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(
        "routstr.upstream.routstr.httpx.AsyncClient",
        client_factory,
    )
    provider = RoutstrUpstreamProvider(
        base_url="https://upstream.example", api_key="sk-old"
    )
    _use_provider(monkeypatch, lambda _row: provider)
    return provider


def _use_provider(
    monkeypatch: pytest.MonkeyPatch,
    instantiate: Callable[[Any], RoutstrUpstreamProvider],
) -> None:
    @asynccontextmanager
    async def fake_session() -> AsyncIterator[Mock]:
        yield Mock()

    monkeypatch.setattr(admin, "create_session", fake_session)
    monkeypatch.setattr(
        admin,
        "_get_upstream_provider_by_ref",
        AsyncMock(return_value=SimpleNamespace(provider_type="routstr")),
    )
    monkeypatch.setattr(upstream_helpers, "_instantiate_provider", instantiate)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {
            "detail": {
                "error": {
                    "message": "No balance to refund",
                    "type": "invalid_request_error",
                    "code": "no_balance_to_refund",
                }
            }
        },
        # Nodes predating structured refund errors answer with a bare string.
        {"detail": "No balance to refund"},
    ],
)
async def test_refund_reports_empty_upstream_key_as_nothing_to_refund(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    _upstream_answering(monkeypatch, 400, body)

    result = await admin.refund_routstr_provider_balance("1")

    assert result["ok"] is True
    assert result["no_balance"] is True
    assert "token" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {
            "detail": {
                "error": {
                    "message": "Balance too small to refund",
                    "type": "invalid_request_error",
                    "code": "balance_too_small_to_refund",
                }
            }
        },
        {"detail": "Balance too small to refund"},
    ],
)
async def test_refund_reports_dust_balance_as_abandoned_remainder(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    _upstream_answering(monkeypatch, 400, body)

    result = await admin.refund_routstr_provider_balance("1")

    assert result["ok"] is True
    assert result["no_balance"] is True
    assert "abandoned" in str(result["message"])


@pytest.mark.asyncio
async def test_refund_keeps_real_upstream_refusal_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upstream_answering(
        monkeypatch,
        400,
        {
            "detail": {
                "error": {
                    "message": "Cannot refund key. There are ongoing requests "
                    "for this api key.",
                    "type": "invalid_request_error",
                    "code": "refund_ongoing_requests",
                }
            }
        },
    )

    result = await admin.refund_routstr_provider_balance("1")

    assert result["ok"] is False
    assert "no_balance" not in result
    assert "ongoing requests" in str(result["message"])


@pytest.mark.asyncio
async def test_refund_keeps_unreachable_upstream_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("All connection attempts failed", request=request)

    _upstream_handled_by(monkeypatch, handler)

    result = await admin.refund_routstr_provider_balance("1")

    assert result == {
        "ok": False,
        "message": "Upstream refund failed: All connection attempts failed",
    }


@pytest.mark.asyncio
async def test_refund_returns_token_the_local_wallet_cannot_receive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upstream_answering(monkeypatch, 200, {"token": "cashuBrefund", "sats": "21"})
    monkeypatch.setattr(
        wallet_module,
        "recieve_token",
        AsyncMock(side_effect=ValueError("Untrusted source mint")),
    )

    result = await admin.refund_routstr_provider_balance("1")

    assert result["ok"] is False
    assert result["token"] == "cashuBrefund"


@pytest.mark.asyncio
async def test_refund_receives_upstream_token_into_local_wallet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upstream_answering(monkeypatch, 200, {"token": "cashuBrefund", "sats": "21"})
    receive = AsyncMock(return_value=(21, "sat", "https://mint.example"))
    monkeypatch.setattr(wallet_module, "recieve_token", receive)

    result = await admin.refund_routstr_provider_balance("1")

    assert result["ok"] is True
    assert "token" not in result
    receive.assert_awaited_once_with("cashuBrefund")


@pytest.mark.asyncio
async def test_refund_reports_replayed_spent_token_as_already_collected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upstream_answering(monkeypatch, 200, {"token": "cashuBrefund", "sats": "21"})
    monkeypatch.setattr(
        wallet_module,
        "recieve_token",
        AsyncMock(
            side_effect=ValueError("Mint Error: Token already spent. (Code: 11001)")
        ),
    )

    result = await admin.refund_routstr_provider_balance("1")

    assert result == {
        "ok": True,
        "no_balance": True,
        "message": "Refund already collected",
    }
