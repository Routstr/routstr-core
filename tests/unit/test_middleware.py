"""Unit tests for middleware functionality."""

import pytest
from fastapi import Request
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_logging_middleware() -> None:
    """Test logging middleware."""
    from routstr.core.middleware import LoggingMiddleware
    
    middleware = LoggingMiddleware(None)
    
    request = Request({"type": "http", "method": "GET", "url": "/test"})
    
    call_next = AsyncMock(return_value=MagicMock(status_code=200))
    
    response = await middleware.dispatch(request, call_next)
    
    assert response.status_code == 200
    call_next.assert_called_once()


@pytest.mark.asyncio
async def test_logging_middleware_error_handling() -> None:
    """Test logging middleware error handling."""
    from routstr.core.middleware import LoggingMiddleware
    
    middleware = LoggingMiddleware(None)
    
    request = Request({"type": "http", "method": "GET", "url": "/test"})
    
    call_next = AsyncMock(side_effect=Exception("Test error"))
    
    with pytest.raises(Exception):
        await middleware.dispatch(request, call_next)


@pytest.mark.asyncio
async def test_middleware_request_id() -> None:
    """Test that middleware adds request ID."""
    from routstr.core.middleware import LoggingMiddleware
    
    middleware = LoggingMiddleware(None)
    
    request = Request({"type": "http", "method": "GET", "url": "/test"})
    
    call_next = AsyncMock(return_value=MagicMock(status_code=200))
    
    await middleware.dispatch(request, call_next)
    
    assert hasattr(request.state, "request_id")
    assert request.state.request_id is not None


@pytest.mark.asyncio
async def test_middleware_logs_request_details() -> None:
    """Test that middleware logs request details."""
    from routstr.core.middleware import LoggingMiddleware
    
    middleware = LoggingMiddleware(None)
    
    request = Request({"type": "http", "method": "POST", "url": "/test"})
    
    call_next = AsyncMock(return_value=MagicMock(status_code=200))
    
    with patch("routstr.core.middleware.logger") as mock_logger:
        await middleware.dispatch(request, call_next)
        
        mock_logger.info.assert_called()
