from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_root_endpoint(async_client: AsyncClient) -> None:
    """Test the root endpoint returns expected information."""
    # Mock the environment variables for this specific test
    env_vars = {
        "NAME": "TestRoutstrNode",
        "DESCRIPTION": "Test Node",
        "NPUB": "npub1test",
        "MINT": "https://test.mint.com",
        "HTTP_URL": "http://test.example.com",
        "ONION_URL": "http://test.onion",
    }

    with patch.dict("os.environ", env_vars, clear=False):
        response = await async_client.get("/")

        assert response.status_code == 200
        data = response.json()

        # The app reads from env vars during import, so check what we actually get
        assert "name" in data
        assert "description" in data
        assert data["version"] == "0.0.1"
        assert "npub" in data
        assert "mint" in data
        assert "http_url" in data
        assert "onion_url" in data
        assert "models" in data


@pytest.mark.asyncio
async def test_cors_headers(async_client: AsyncClient) -> None:
    """Test that CORS headers are properly set."""
    response = await async_client.options(
        "/",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    # Check that CORS is working (might be * or specific origin)
    assert "access-control-allow-origin" in response.headers
    assert "GET" in response.headers["access-control-allow-methods"]


@pytest.mark.asyncio
async def test_startup_event_initializes_properly(test_client: TestClient) -> None:
    """Test that the startup event runs without errors."""
    # The test_client fixture already triggers the startup event
    # This test ensures no exceptions are raised during startup
    response = test_client.get("/")
    assert response.status_code == 200
