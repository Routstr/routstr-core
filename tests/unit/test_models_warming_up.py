"""Unknown models get a retryable 503 while startup is still loading providers."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Response

from routstr import proxy as proxy_module
from routstr.core.error_scope import ERROR_SCOPE_HEADER, ERROR_SCOPE_NODE

from .test_model_path_routing import _make_request, _run_proxy


@pytest.fixture(autouse=True)
def _reset_warming_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proxy_module, "_warming_up", False)


def _chat_request() -> MagicMock:
    return _make_request(
        {"authorization": "Bearer sk-mpkey"},
        json.dumps({"model": "not-loaded-yet"}).encode(),
    )


@pytest.mark.asyncio
async def test_unknown_model_while_warming_up_is_retryable_503() -> None:
    proxy_module.mark_warming_up()

    response = await _run_proxy(_chat_request(), [])

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "2"
    assert response.headers[ERROR_SCOPE_HEADER] == ERROR_SCOPE_NODE
    assert json.loads(bytes(response.body))["error"]["code"] == "MODELS_WARMING_UP"


@pytest.mark.asyncio
async def test_unknown_model_after_warm_up_is_invalid_model() -> None:
    response = await _run_proxy(_chat_request(), [])

    assert response.status_code == 400
    assert json.loads(bytes(response.body))["error"]["type"] == "invalid_model"


@pytest.mark.asyncio
async def test_failed_initialization_still_ends_warm_up() -> None:
    proxy_module.mark_warming_up()

    with (
        patch.object(
            proxy_module, "init_upstreams", AsyncMock(side_effect=RuntimeError)
        ),
        pytest.raises(RuntimeError),
    ):
        await proxy_module.initialize_upstreams()

    assert not proxy_module.is_warming_up()


@pytest.mark.asyncio
async def test_models_list_while_warming_up_is_retryable_503() -> None:
    from routstr.payment.models import models

    proxy_module.mark_warming_up()

    response = await models(_chat_request(), MagicMock())

    assert isinstance(response, Response)
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "2"


@pytest.mark.asyncio
async def test_models_list_after_warm_up_returns_data() -> None:
    from routstr.payment.models import models

    with patch.object(proxy_module, "get_unique_models", return_value=[]):
        response = await models(_chat_request(), MagicMock())

    assert response == {"data": []}
