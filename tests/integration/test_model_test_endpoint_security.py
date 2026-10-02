import time
from types import TracebackType
from typing import Any
from unittest.mock import patch

import pytest
from httpx import AsyncClient

from routstr.core.admin import admin_sessions
from routstr.core.db import ModelRow, UpstreamProviderRow


@pytest.mark.integration
@pytest.mark.asyncio
async def test_model_test_endpoint_requires_admin_auth(
    integration_client: AsyncClient,
) -> None:
    with patch("httpx.AsyncClient") as mock_async_client:
        response = await integration_client.post(
            "/api/models/test",
            json={
                "model_id": "model-a",
                "endpoint_type": "chat-completions",
                "request_data": {"messages": []},
            },
        )

    assert response.status_code == 403
    mock_async_client.assert_not_called()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_model_test_endpoint_rejects_unsupported_endpoint_type(
    integration_client: AsyncClient,
    integration_session: Any,
) -> None:
    admin_token = "test-admin-token-model-test"
    admin_sessions[admin_token] = int(time.time()) + 3600
    integration_client.headers["Authorization"] = f"Bearer {admin_token}"

    provider = UpstreamProviderRow(
        provider_type="custom",
        base_url="https://api.example.com/v1",
        api_key="sk-upstream-test",
        enabled=True,
        provider_fee=1.01,
    )
    integration_session.add(provider)
    await integration_session.commit()
    await integration_session.refresh(provider)
    assert provider.id is not None

    model = ModelRow(
        id="model-a",
        name="Model A",
        created=1,
        description="desc",
        context_length=100,
        architecture='{"modality": "text", "input_modalities": ["text"], "output_modalities": ["text"], "tokenizer": "tiktoken", "instruct_type": "chat"}',
        pricing='{"prompt": 1.0, "completion": 1.0}',
        upstream_provider_id=provider.id,
        enabled=True,
    )
    integration_session.add(model)
    await integration_session.commit()

    try:
        with patch("httpx.AsyncClient") as mock_async_client:
            response = await integration_client.post(
                "/api/models/test",
                json={
                    "model_id": "model-a",
                    "endpoint_type": "../../abuse",
                    "request_data": {"messages": []},
                },
            )

        assert response.status_code == 400
        assert response.json()["detail"] == "Unsupported endpoint_type"
        mock_async_client.assert_not_called()
    finally:
        admin_sessions.pop(admin_token, None)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_model_test_endpoint_rejects_oversized_request_data(
    integration_client: AsyncClient,
    integration_session: Any,
) -> None:
    admin_token = "test-admin-token-model-test-oversized"
    admin_sessions[admin_token] = int(time.time()) + 3600
    integration_client.headers["Authorization"] = f"Bearer {admin_token}"

    provider = UpstreamProviderRow(
        provider_type="custom",
        base_url="https://api.example.com/v1",
        api_key="sk-upstream-test",
        enabled=True,
        provider_fee=1.01,
    )
    integration_session.add(provider)
    await integration_session.commit()
    await integration_session.refresh(provider)
    assert provider.id is not None

    model = ModelRow(
        id="model-a",
        name="Model A",
        created=1,
        description="desc",
        context_length=100,
        architecture='{"modality": "text", "input_modalities": ["text"], "output_modalities": ["text"], "tokenizer": "tiktoken", "instruct_type": "chat"}',
        pricing='{"prompt": 1.0, "completion": 1.0}',
        upstream_provider_id=provider.id,
        enabled=True,
    )
    integration_session.add(model)
    await integration_session.commit()

    oversized = "x" * (64 * 1024 + 1)

    try:
        with patch("httpx.AsyncClient") as mock_async_client:
            response = await integration_client.post(
                "/api/models/test",
                json={
                    "model_id": "model-a",
                    "endpoint_type": "chat-completions",
                    "request_data": {"blob": oversized},
                },
            )

        assert response.status_code == 413
        assert response.json()["detail"] == "request_data too large"
        mock_async_client.assert_not_called()
    finally:
        admin_sessions.pop(admin_token, None)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_model_test_endpoint_admin_uses_allowed_upstream_path(
    integration_client: AsyncClient,
    integration_session: Any,
) -> None:
    admin_token = "test-admin-token-model-test-success"
    admin_sessions[admin_token] = int(time.time()) + 3600
    integration_client.headers["Authorization"] = f"Bearer {admin_token}"

    provider = UpstreamProviderRow(
        provider_type="custom",
        base_url="https://api.example.com/v1",
        api_key="sk-upstream-test",
        enabled=True,
        provider_fee=1.01,
    )
    integration_session.add(provider)
    await integration_session.commit()
    await integration_session.refresh(provider)
    assert provider.id is not None

    model = ModelRow(
        id="model-a",
        name="Model A",
        created=1,
        description="desc",
        context_length=100,
        architecture='{"modality": "text", "input_modalities": ["text"], "output_modalities": ["text"], "tokenizer": "tiktoken", "instruct_type": "chat"}',
        pricing='{"prompt": 1.0, "completion": 1.0}',
        upstream_provider_id=provider.id,
        enabled=True,
        forwarded_model_id="upstream-model-a",
    )
    integration_session.add(model)
    await integration_session.commit()

    class MockResponse:
        status_code = 200
        text = '{"ok": true}'

        def json(self) -> dict[str, bool]:
            return {"ok": True}

    class MockAsyncClient:
        async def __aenter__(self) -> "MockAsyncClient":
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> None:
            return None

        async def post(
            self, url: str, json: dict[str, Any], headers: dict[str, str]
        ) -> MockResponse:
            assert url == "https://api.example.com/v1/chat/completions"
            assert json["model"] == "upstream-model-a"
            assert headers["Authorization"] == "Bearer sk-upstream-test"
            return MockResponse()

    try:
        with patch("httpx.AsyncClient", return_value=MockAsyncClient()):
            response = await integration_client.post(
                "/api/models/test",
                json={
                    "model_id": "model-a",
                    "endpoint_type": "chat-completions",
                    "request_data": {"messages": []},
                },
            )

        assert response.status_code == 200
        assert response.json() == {
            "success": True,
            "data": {"ok": True},
            "status_code": 200,
        }
    finally:
        admin_sessions.pop(admin_token, None)
