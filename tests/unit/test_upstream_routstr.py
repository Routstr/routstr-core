from types import TracebackType
from unittest.mock import Mock

import httpx
import pytest

from routstr.upstream.routstr import RoutstrUpstreamProvider


class DummyAsyncClient:
    def __init__(self, response: Mock | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def __aenter__(self) -> "DummyAsyncClient":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        return False

    async def get(
        self, url: str, headers: dict[str, str], timeout: float
    ) -> Mock:
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


@pytest.mark.asyncio
async def test_get_balance_omits_auth_header_when_api_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = Mock()
    response.json.return_value = {"balance_msats": 42000}
    response.raise_for_status.return_value = None

    client = DummyAsyncClient(response=response)
    monkeypatch.setattr("routstr.upstream.routstr.httpx.AsyncClient", lambda: client)

    provider = RoutstrUpstreamProvider(base_url="https://node.example", api_key="")

    balance = await provider.get_balance()

    assert balance == 42.0
    assert client.calls == [
        {
            "url": "https://node.example/v1/balance/info",
            "headers": {},
            "timeout": 10.0,
        }
    ]


@pytest.mark.asyncio
async def test_get_balance_returns_none_on_connect_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DummyAsyncClient(error=httpx.ConnectTimeout("timed out"))
    monkeypatch.setattr("routstr.upstream.routstr.httpx.AsyncClient", lambda: client)

    provider = RoutstrUpstreamProvider(
        base_url="https://node.example",
        api_key="secret",
    )

    balance = await provider.get_balance()

    assert balance is None
