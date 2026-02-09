from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from routstr.websearch.http_client import HTTPClient


@pytest.mark.asyncio
async def test_http_client_status_error_handling() -> None:
    """Verify that HTTP status errors are caught and re-raised as provider errors."""
    client = HTTPClient()
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 404

    # Mock the behavior of raise_for_status triggering an HTTPStatusError
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "404 Error", request=MagicMock(), response=mock_response
    )

    with patch.object(
        client._client, "request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.return_value = mock_response

        with pytest.raises(Exception, match="Provider Error: 404"):
            await client.get("https://broken.com")


@pytest.mark.asyncio
async def test_http_client_network_failure_handling() -> None:
    """Verify that low-level network failures are caught and re-raised as network failure errors."""
    client = HTTPClient()

    with patch.object(
        client._client, "request", new_callable=AsyncMock
    ) as mock_request:
        # Simulate a network-level RequestError
        mock_request.side_effect = httpx.RequestError("Connection Refused")

        with pytest.raises(Exception, match="Network Failure"):
            await client.get("https://offline.com")


@pytest.mark.asyncio
async def test_http_client_post_request_mapping() -> None:
    """Ensure POST data is correctly passed to the underlying request client."""
    client = HTTPClient()
    payload = {"query": "bitcoin"}

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 201
    mock_response.json.return_value = {"success": True}

    with patch.object(
        client._client, "request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.return_value = mock_response

        await client.post("https://api.com/v1", json_data=payload)

        # Check that internal arguments match the expected library call
        args, kwargs = mock_request.call_args
        assert kwargs["json"] == payload
        assert kwargs["method"] == "POST"
