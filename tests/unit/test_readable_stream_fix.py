import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
from fastapi import Request
from fastapi.responses import StreamingResponse, Response

from routstr.payment.x_cashu import (
    safe_read_response_content,
    handle_x_cashu_chat_completion,
    send_refund,
)


class TestReadableStreamErrorHandling:
    """Test cases for ReadableStream error handling in CoinOS with nonkycai provider."""

    @pytest.mark.asyncio
    async def test_safe_read_response_content_with_readable_stream(self):
        """Test that ReadableStream objects are properly detected and handled."""
        # Mock response with ReadableStream content
        mock_response = MagicMock()
        mock_response.aread = AsyncMock(return_value=b"[object ReadableStream]")
        mock_response.headers = {"content-type": "text/plain"}
        mock_response.status_code = 200

        content_str, is_readable_stream_error = await safe_read_response_content(mock_response)
        
        assert content_str is None
        assert is_readable_stream_error is True

    @pytest.mark.asyncio
    async def test_safe_read_response_content_with_normal_content(self):
        """Test that normal content is processed correctly."""
        # Mock response with normal JSON content
        mock_response = MagicMock()
        mock_response.aread = AsyncMock(return_value=b'{"message": "Hello, world!"}')
        mock_response.headers = {"content-type": "application/json"}
        mock_response.status_code = 200

        content_str, is_readable_stream_error = await safe_read_response_content(mock_response)
        
        assert content_str == '{"message": "Hello, world!"}'
        assert is_readable_stream_error is False

    @pytest.mark.asyncio
    async def test_safe_read_response_content_with_streaming_data(self):
        """Test that streaming SSE content is processed correctly."""
        # Mock response with SSE streaming content
        sse_content = """data: {"id": "chatcmpl-123", "object": "chat.completion.chunk"}

data: {"id": "chatcmpl-123", "choices": [{"delta": {"content": "Hello"}}]}

data: [DONE]
"""
        mock_response = MagicMock()
        mock_response.aread = AsyncMock(return_value=sse_content.encode())
        mock_response.headers = {"content-type": "text/event-stream"}
        mock_response.status_code = 200

        content_str, is_readable_stream_error = await safe_read_response_content(mock_response)
        
        assert content_str == sse_content
        assert is_readable_stream_error is False

    @pytest.mark.asyncio
    async def test_safe_read_response_content_with_partial_readable_stream(self):
        """Test detection of ReadableStream in partial content."""
        # Mock response with content containing ReadableStream
        mock_response = MagicMock()
        mock_response.aread = AsyncMock(return_value=b"Error: [object ReadableStream] encountered")
        mock_response.headers = {"content-type": "text/plain"}
        mock_response.status_code = 500

        content_str, is_readable_stream_error = await safe_read_response_content(mock_response)
        
        assert content_str is None
        assert is_readable_stream_error is True

    @pytest.mark.asyncio
    @patch('routstr.payment.x_cashu.send_refund')
    @patch('routstr.payment.x_cashu.safe_read_response_content')
    async def test_handle_chat_completion_with_readable_stream_error(
        self, mock_safe_read, mock_send_refund
    ):
        """Test that ReadableStream errors trigger emergency refunds."""
        # Setup mocks
        mock_safe_read.return_value = (None, True)  # ReadableStream error
        mock_send_refund.return_value = "cashuAtest123refund"
        
        # Mock response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/event-stream"}
        mock_response.aiter_bytes = AsyncMock(return_value=iter([b"data: test"]))

        # Test parameters
        amount = 1000
        unit = "sat"
        max_cost_for_model = 500

        result = await handle_x_cashu_chat_completion(
            mock_response, amount, unit, max_cost_for_model
        )

        # Verify emergency refund was called
        mock_send_refund.assert_called_once_with(940, unit)  # amount - 60
        
        # Verify response is StreamingResponse with refund header
        assert isinstance(result, StreamingResponse)
        assert result.headers["X-Cashu"] == "cashuAtest123refund"

    @pytest.mark.asyncio
    @patch('routstr.payment.x_cashu.send_refund')
    @patch('routstr.payment.x_cashu.safe_read_response_content')
    async def test_handle_chat_completion_refund_failure(
        self, mock_safe_read, mock_send_refund
    ):
        """Test handling when both ReadableStream error and refund fail."""
        # Setup mocks
        mock_safe_read.return_value = (None, True)  # ReadableStream error
        mock_send_refund.side_effect = Exception("Refund failed")
        
        # Mock response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/event-stream"}

        # Test parameters
        amount = 1000
        unit = "sat"
        max_cost_for_model = 500

        result = await handle_x_cashu_chat_completion(
            mock_response, amount, unit, max_cost_for_model
        )

        # Verify error response is returned
        assert isinstance(result, Response)
        assert result.status_code == 500
        
        # Verify error content
        content = json.loads(result.body)
        assert content["error"]["code"] == "readable_stream_error"
        assert content["error"]["original_amount"] == amount
        assert content["error"]["unit"] == unit

    @pytest.mark.asyncio
    @patch('routstr.payment.x_cashu.send_token')
    async def test_send_refund_with_retries(self, mock_send_token):
        """Test that send_refund retries on failure."""
        # Setup mock to fail twice then succeed
        mock_send_token.side_effect = [
            Exception("Network error"),
            Exception("Temporary failure"),
            "cashuAtest123refund"
        ]

        result = await send_refund(500, "sat")
        
        assert result == "cashuAtest123refund"
        assert mock_send_token.call_count == 3

    @pytest.mark.asyncio
    @patch('routstr.payment.x_cashu.send_token')
    async def test_send_refund_max_retries_exceeded(self, mock_send_token):
        """Test that send_refund raises HTTPException after max retries."""
        # Setup mock to always fail
        mock_send_token.side_effect = Exception("Persistent failure")

        with pytest.raises(Exception) as exc_info:
            await send_refund(500, "sat")
        
        # Verify all retries were attempted
        assert mock_send_token.call_count == 3

    @pytest.mark.asyncio
    async def test_safe_read_response_content_with_unicode_errors(self):
        """Test handling of invalid UTF-8 content."""
        # Mock response with invalid UTF-8 bytes
        mock_response = MagicMock()
        mock_response.aread = AsyncMock(return_value=b'\xff\xfe\x00\x00invalid utf-8')
        mock_response.headers = {"content-type": "text/plain"}
        mock_response.status_code = 200

        content_str, is_readable_stream_error = await safe_read_response_content(mock_response)
        
        # Should handle invalid UTF-8 gracefully with replacement characters
        assert content_str is not None
        assert is_readable_stream_error is False
        assert "invalid utf-8" in content_str

    @pytest.mark.asyncio
    async def test_safe_read_response_content_with_exception(self):
        """Test handling when aread() raises an exception."""
        # Mock response that raises exception on aread
        mock_response = MagicMock()
        mock_response.aread = AsyncMock(side_effect=Exception("Connection lost"))
        mock_response.headers = {"content-type": "text/plain"}
        mock_response.status_code = 200

        content_str, is_readable_stream_error = await safe_read_response_content(mock_response)
        
        assert content_str is None
        assert is_readable_stream_error is False


class TestCoinOSNonKYCAIIntegration:
    """Integration tests specifically for CoinOS with nonkycai provider scenarios."""

    @pytest.mark.asyncio
    @patch('routstr.payment.x_cashu.safe_read_response_content')
    @patch('routstr.payment.x_cashu.send_refund')
    async def test_coinos_nonkycai_readable_stream_scenario(
        self, mock_send_refund, mock_safe_read
    ):
        """Test the specific CoinOS + nonkycai ReadableStream scenario."""
        # Simulate the exact error scenario from the issue
        mock_safe_read.return_value = (None, True)  # ReadableStream detected
        mock_send_refund.return_value = "cashuAemergencyrefund123"
        
        # Mock response that would come from nonkycai provider
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {
            "content-type": "text/event-stream",
            "cache-control": "no-cache",
            "connection": "keep-alive"
        }
        mock_response.aiter_bytes = AsyncMock(return_value=iter([
            b"data: [object ReadableStream]\n\n"
        ]))

        # Simulate CoinOS payment parameters
        amount = 2100  # 2100 sats
        unit = "sat"
        max_cost_for_model = 1000

        result = await handle_x_cashu_chat_completion(
            mock_response, amount, unit, max_cost_for_model
        )

        # Verify emergency refund was issued
        mock_send_refund.assert_called_once_with(2040, unit)  # 2100 - 60
        
        # Verify proper response handling
        assert isinstance(result, StreamingResponse)
        assert "X-Cashu" in result.headers
        assert result.headers["X-Cashu"] == "cashuAemergencyrefund123"
        assert result.status_code == 200

    @pytest.mark.asyncio
    async def test_readable_stream_detection_variations(self):
        """Test detection of various ReadableStream object representations."""
        test_cases = [
            b"[object ReadableStream]",
            b"Error: [object ReadableStream] encountered",
            b"Response contains ReadableStream object",
            b"ReadableStream processing failed",
            b'{"error": "ReadableStream not supported"}',
        ]

        for content in test_cases:
            mock_response = MagicMock()
            mock_response.aread = AsyncMock(return_value=content)
            mock_response.headers = {"content-type": "text/plain"}
            mock_response.status_code = 200

            content_str, is_readable_stream_error = await safe_read_response_content(mock_response)
            
            assert is_readable_stream_error is True, f"Failed to detect ReadableStream in: {content}"
            assert content_str is None