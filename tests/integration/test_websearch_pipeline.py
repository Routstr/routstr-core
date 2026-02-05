"""
Test suite for Web Search (RAG) integration within the proxy layer.

This module validates the end-to-end flow of Retrieval-Augmented Generation (RAG),
specifically verifying how web search results are injected into prompts and how
the sources are returned in the Response. It covers both streaming and non-streaming
responses across standard Bearer authentication and X-Cashu payment protocols.
"""

import json
import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import Request
from routstr.payment.models import Model, Pricing, Architecture, TopProvider
from routstr.core.settings import settings
from routstr.upstream.base import BaseUpstreamProvider
from routstr.proxy import proxy
from routstr.websearch.web_manager import WebManager 
from routstr.websearch.types import SearchResult, WebPage

# --- Fixtures ---

@pytest.fixture
def model_id():
    return "gpt-3.5-turbo"

@pytest.fixture
def mock_model(model_id):
    return Model(
        id=model_id,
        name=model_id,
        created=123456789,
        description="Test model",
        context_length=4096,
        architecture=Architecture(
            modality="text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="gpt-3",
            instruct_type=None
        ),
        pricing=Pricing(prompt=0.00001, completion=0.00002, request=0, image=0, web_search=0, internal_reasoning=0),
        sats_pricing=Pricing(prompt=0.001, completion=0.002, request=0, image=0, web_search=10, internal_reasoning=0),
        per_request_limits=None,
        top_provider=TopProvider(context_length=4096, max_completion_tokens=2048, is_moderated=False),
        enabled=True,
        upstream_provider_id=1,
        canonical_slug=model_id,
        alias_ids=[]
    )

@pytest.fixture
def mock_key():
    key = MagicMock()
    key.hashed_key = "test-key-hash"
    key.balance = 1000000
    return key

@pytest.fixture
def mock_search_result():
    # Matches the SearchResult format
    return SearchResult(
        query="Who won the Super Bowl?",
        webpages=[
            WebPage(
                title="Super Bowl Chiefs",
                url="https://espn.com/nfl",
                content="The Chiefs won the Super Bowl.",
                summary="The Chiefs won the Super Bowl.",
                chunks=["The Chiefs won the Super Bowl."]
            )
        ]
    )

@pytest.fixture
def mock_upstream_response_json(model_id):
    return {
        "model": model_id,
        "choices": [{"message": {"content": "The Chiefs won."}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
    }

@pytest.fixture
def setup_mocks(mock_model, mock_key, mock_search_result):
    """Setup common mocks for all tests using standard patch."""
    
    async def simulate_successful_web_search(request_body: bytes):
        enhanced_body, sources = await WebManager.inject_web_context_into_request(
            request_body=request_body,
            search_result=mock_search_result,
            query="Who won the Super Bowl?"
        )
        return {"body": enhanced_body, "sources": sources, "success": True}

    with patch("routstr.proxy.get_model_instance", return_value=mock_model), \
         patch("routstr.proxy.validate_bearer_key", new_callable=AsyncMock, return_value=mock_key), \
         patch("routstr.proxy.get_max_cost_for_model", new_callable=AsyncMock, return_value=5000), \
         patch("routstr.proxy.calculate_discounted_max_cost", new_callable=AsyncMock, return_value=5000), \
         patch("routstr.proxy.pay_for_request", new_callable=AsyncMock), \
         patch("routstr.proxy.check_token_balance", return_value=None), \
         patch("routstr.upstream.base.WebManager.is_rag_enabled", return_value=True), \
         patch("routstr.upstream.base.web_manager", new_callable=AsyncMock) as mock_wm, \
         patch("routstr.upstream.base.adjust_payment_for_tokens", new_callable=AsyncMock, return_value={
             "base_msats": 0, 
             "input_msats": 20, 
             "output_msats": 20,
             "web_search_msats": 1000, 
             "total_msats": 1040
         }), \
         patch("routstr.upstream.base.AsyncSession.get", return_value=mock_key), \
         patch.object(settings, "web_rag_provider", "tavily"), \
         patch.object(settings, "fixed_pricing", False), \
         patch.object(settings, "web_search_fixed_cost", 1000):
        
        mock_wm.enhance_request_with_web_context.side_effect = simulate_successful_web_search

        provider = BaseUpstreamProvider(base_url="http://upstream", api_key="test-key")
        
        with patch("routstr.proxy.get_provider_for_model", return_value=provider):
            yield provider

# --- Tests ---

@pytest.mark.asyncio
async def test_websearch_non_streaming_auth(setup_mocks, model_id, mock_upstream_response_json):
    """Test non-streaming response with Authorization header."""
    test_payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "Who won the Super Bowl?"}],
        "stream": False,
        "enable_web_search": True
    }
    
    mock_request = MagicMock(spec=Request)
    mock_request.method = "POST"
    mock_request.headers = {"authorization": "Bearer test-key", "content-type": "application/json"}
    mock_request.body = AsyncMock(return_value=json.dumps(test_payload).encode())
    mock_request.query_params = {}

    mock_response = httpx.Response(
        200, 
        headers={"content-type": "application/json"}, 
        content=json.dumps(mock_upstream_response_json).encode()
    )
    
    with patch("httpx.AsyncClient.send", new_callable=AsyncMock, return_value=mock_response) as mock_send:
        response = await proxy(request=mock_request, path="v1/chat/completions", session=AsyncMock())

        assert response.status_code == 200
        data = json.loads(response.body)
        # Check if web_search_executed flag is present
        assert data["web_search_executed"] is True
        # Check if sources are present in response
        assert "sources" in data
        # Check if Sources are correct
        assert data["sources"] == {"1": "https://espn.com/nfl"}
        
        forwarded_request = mock_send.call_args[0][0]
        forwarded_body = json.loads(forwarded_request.content)
        
        # Check if context was successfully injected 
        assert "The Chiefs won the Super Bowl." in forwarded_body["messages"][0]["content"]

@pytest.mark.asyncio
async def test_websearch_streaming_auth(setup_mocks, model_id):
    """Test streaming response with Authorization header."""
    test_payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "Who won the Super Bowl?"}],
        "stream": True,
        "enable_web_search": True
    }
    
    mock_request = MagicMock(spec=Request)
    mock_request.method = "POST"
    mock_request.headers = {"authorization": "Bearer test-key", "content-type": "application/json"}
    mock_request.body = AsyncMock(return_value=json.dumps(test_payload).encode())
    mock_request.query_params = {}

    async def mock_aiter_bytes():
        yield b'data: {"choices": [{"delta": {"content": "The Chiefs"}}], "model": "gpt-3.5-turbo"}\n\n'
        yield b'data: {"choices": [{"delta": {"content": " won."}}], "model": "gpt-3.5-turbo", "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}}\n\n'
        yield b'data: [DONE]\n\n'

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "text/event-stream"}
    mock_response.aiter_bytes = mock_aiter_bytes
    
    with patch("httpx.AsyncClient.send", new_callable=AsyncMock, return_value=mock_response):
        response = await proxy(request=mock_request, path="v1/chat/completions", session=AsyncMock())

        assert response.status_code == 200
        
        chunks = [chunk async for chunk in response.body_iterator]
        data = json.loads(chunks[-1].decode().removeprefix("data: "))
        print(data)
        # Check if web_search_executed flag is present
        assert data["web_search_executed"] is True
        # Check if sources are present in response
        assert "sources" in data
        # Check if Sources are correct
        assert data["sources"] == {"1": "https://espn.com/nfl"}

        


@pytest.mark.asyncio
async def test_websearch_streaming_xcashu(setup_mocks, model_id):
    """Test streaming response with X-Cashu header."""
    test_payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "Who won the Super Bowl?"}],
        "stream": True,
        "enable_web_search": True
    }
    
    mock_request = MagicMock(spec=Request)
    mock_request.method = "POST"
    mock_request.headers = {"x-cashu": "test-token", "content-type": "application/json"}
    mock_request.body = AsyncMock(return_value=json.dumps(test_payload).encode())
    mock_request.query_params = {}

    async def mock_aiter_bytes():
        yield b'data: {"choices": [{"delta": {"content": "The Chiefs"}}], "model": "gpt-3.5-turbo"}\n\n'
        yield b'data: {"choices": [{"delta": {"content": " won."}}], "model": "gpt-3.5-turbo", "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}}\n\n'
        yield b'data: [DONE]\n\n'

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "text/event-stream"}
    mock_response.aiter_bytes = mock_aiter_bytes
    mock_response.aread = AsyncMock(return_value=b"".join([c async for c in mock_aiter_bytes()]))
    
    mock_cost_data = MagicMock()
    mock_cost_data.total_msats = 1040

    with patch("routstr.upstream.base.recieve_token", new_callable=AsyncMock, return_value=(5000, "msat", "http://mint")), \
         patch("routstr.upstream.base.send_token", new_callable=AsyncMock, return_value="refund-token"), \
         patch("httpx.AsyncClient.send", new_callable=AsyncMock, return_value=mock_response):
        
        response = await proxy(request=mock_request, path="v1/chat/completions", session=AsyncMock())

        assert response.status_code == 200
        
        chunks = [chunk async for chunk in response.body_iterator]
        data = json.loads(chunks[-1].decode().removeprefix("data: "))

        # X-Cashu responses do not return Cost object 
        # Instead it just returns the refund token in the X-Cashu header
        assert response.headers["X-Cashu"] == "refund-token"

        # Check if web_search_executed flag is present
        assert data["web_search_executed"] is True
        # Check if sources are present in response
        assert "sources" in data
        # Check if Sources are correct
        assert data["sources"] == {"1": "https://espn.com/nfl"}
        # Check if websearch cost was returned and correct

@pytest.mark.asyncio
async def test_websearch_non_streaming_xcashu(setup_mocks, model_id, mock_upstream_response_json):
    """Test non-streaming response with X-Cashu header."""
    test_payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "Who won the Super Bowl?"}],
        "stream": False,
        "enable_web_search": True
    }
    
    mock_request = MagicMock(spec=Request)
    mock_request.method = "POST"
    mock_request.headers = {"x-cashu": "test-token", "content-type": "application/json"}
    mock_request.body = AsyncMock(return_value=json.dumps(test_payload).encode())
    mock_request.query_params = {}

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "application/json"}
    mock_response.aread = AsyncMock(return_value=json.dumps(mock_upstream_response_json).encode())
    
    mock_cost_data = MagicMock()
    mock_cost_data.total_msats = 1040

    with patch("routstr.upstream.base.recieve_token", new_callable=AsyncMock, return_value=(5000, "msat", "http://mint")), \
         patch("routstr.upstream.base.send_token", new_callable=AsyncMock, return_value="refund-token"), \
         patch("httpx.AsyncClient.send", new_callable=AsyncMock, return_value=mock_response) as mock_send:
         
        response = await proxy(request=mock_request, path="v1/chat/completions", session=AsyncMock())

        assert response.status_code == 200
        data = json.loads(response.body)
        # Check if web_search_executed flag is present
        assert data["web_search_executed"] is True
        
        # X-Cashu responses do not return Cost object 
        # Instead it just returns the refund token in the X-Cashu header
        assert response.headers["X-Cashu"] == "refund-token"

        # Check if sources are present in response
        assert "sources" in data
        # Check if Sources are correct
        assert data["sources"] == {"1": "https://espn.com/nfl"}

        forwarded_request = mock_send.call_args[0][0]
        forwarded_body = json.loads(forwarded_request.content)
        
        # Check if context was successfully injected 
        assert "The Chiefs won the Super Bowl." in forwarded_body["messages"][0]["content"]
