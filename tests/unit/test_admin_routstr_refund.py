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
async def test_refund_reports_dust_balance_as_abandoned_remainder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upstream_answering(
        monkeypatch,
        400,
        {
            "detail": {
                "error": {
                    "message": "Balance too small to refund",
                    "type": "invalid_request_error",
                    "code": "balance_too_small_to_refund",
                }
            }
        },
    )

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
    provider = RoutstrUpstreamProvider(
        base_url="https://upstream.example", api_key="sk-old"
    )
    monkeypatch.setattr(
        provider,
        "refund_balance",
        AsyncMock(return_value={"error": "All connection attempts failed"}),
    )
    _use_provider(monkeypatch, lambda _row: provider)

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
