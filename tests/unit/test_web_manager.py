import json
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest

from routstr.core.settings import settings
from routstr.websearch.bm25_ranker import BM25Ranker
from routstr.websearch.custom_web_rag import CustomWebRAG
from routstr.websearch.exa_web_rag import ExaWebRAG
from routstr.websearch.fixed_size_chunker import FixedSizeChunker
from routstr.websearch.http_web_scraper import HTTPWebScraper
from routstr.websearch.recursive_chunker import RecursiveChunker
from routstr.websearch.serper_web_searcher import SerperWebSearcher
from routstr.websearch.tavily_web_rag import TavilyWebRAG
from routstr.websearch.types import (
    SearchResult,
    WebPage,
)
from routstr.websearch.web_manager import WebManager


@pytest.mark.asyncio
async def test_is_rag_enabled_logic() -> None:
    """Test that the manager correctly interprets the RAG_PROVIDER setting."""
    manager = WebManager()

    # Test Disabled
    settings.web_rag_provider = "disabled"
    assert manager.is_rag_enabled() is False

    # Test Enabled (case insensitive)
    settings.web_rag_provider = "TAVILY"
    assert manager.is_rag_enabled() is True

    # Test Missing (Empty String)
    settings.web_rag_provider = ""
    assert manager.is_rag_enabled() is False

    # Test Missing (None)
    settings.web_rag_provider = ""
    assert manager.is_rag_enabled() is False


@pytest.mark.asyncio
async def test_get_rag_provider_caching() -> None:
    """Verify that the manager caches provider instances (Singleton pattern)."""
    manager = WebManager()
    settings.web_rag_provider = "tavily"
    settings.tavily_api_key = "test_key"

    with patch.object(
        TavilyWebRAG, "check_availability", new_callable=AsyncMock
    ) as mock_check_availability:
        mock_check_availability.return_value = True

        provider1 = await manager.get_rag_provider()
        provider2 = await manager.get_rag_provider()

        # Ensure it returns the exact same instance
        assert provider1 is provider2
        # Ensure availability was only checked once (on initialization)
        assert mock_check_availability.call_count == 1


@pytest.mark.asyncio
async def test_custom_rag_initialization_failure() -> None:
    """Test that CustomRAG fails if sub-components (search/scrape) are missing."""
    manager = WebManager()
    settings.web_rag_provider = "custom"

    # Force search provider to be None by having no API key
    settings.web_search_provider = "serper"
    settings.serper_api_key = ""

    provider = await manager.get_rag_provider()
    assert provider is None


@pytest.mark.asyncio
async def test_extract_query_from_request_body() -> None:
    """Test the logic that pulls the last user message to use as a search query."""
    manager = WebManager()
    # System -> User (old) -> Assistant -> User (newest) -> System (instruction)
    history = {
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is Routstr"},
            {
                "role": "assistant",
                "content": "Routstr enables Pay-per-request AI Inference with Bitcoin micropayments!",
            },
            {
                "role": "user",
                "content": "Does it use Cashu?",
            },  # This is the target query
            {
                "role": "system",
                "content": "Instruction: Be brief and use a neutral tone.",
            },
        ]
    }
    sample_body = json.dumps(history).encode("utf-8")
    query = manager.extract_query_from_request_body(sample_body)
    assert query == "Does it use Cashu?"


@pytest.mark.asyncio
async def test_extract_web_search_parameter() -> None:
    """Test the extraction and removal of the custom 'enable_web_search' flag."""
    manager = WebManager()
    body = b'{"model": "gpt-4", "messages": [], "enable_web_search": true}'

    cleaned_body, enabled = manager.extract_web_search_parameter(body)

    import json

    assert cleaned_body is not None

    cleaned_dict = json.loads(cleaned_body)

    assert enabled is True
    assert "enable_web_search" not in cleaned_dict
    assert cleaned_dict["model"] == "gpt-4"


@pytest.mark.asyncio
async def test_get_web_search_provider_resolution() -> None:
    """Verify that the correct search provider class is instantiated based on settings."""
    manager = WebManager()
    settings.web_search_provider = "serper"
    settings.serper_api_key = "test_serper_key"

    provider = await manager.get_web_search_provider()
    from routstr.websearch.serper_web_searcher import SerperWebSearcher

    assert isinstance(provider, SerperWebSearcher)
    assert provider.api_key == "test_serper_key"


@pytest.mark.asyncio
async def test_generate_xml_context_complex_validation() -> None:
    """
    Verify the XML generation
    """
    from routstr.websearch.types import SearchResult, WebPage

    manager = WebManager()

    # Page with full metadata
    page_full = WebPage(
        url="https://comprehensive.com",
        title="Full Article",
        publication_date="2026-01-17",
        summary="This is a brief overview of the article.",  # Page Summary
        chunks=["Content chunk 1.", "Content chunk 2."],
    )

    # Page without additional metadata
    page_no_sum = WebPage(
        url="https://no-summary.com", title="Just Content", chunks=["Only text here."]
    )

    # SearchResult with a global summary
    mock_result = SearchResult(
        query="bitcoin news",
        webpages=[page_full, page_no_sum],
        summary="Bitcoin reached a new all-time high today.",  # Global Summary
    )

    xml = manager.generate_xml_context(mock_result, "bitcoin news")

    # Global Summary Check
    assert (
        "<search_summary>Bitcoin reached a new all-time high today.</search_summary>"
        in xml
    )

    # Source 1: Check Page Summary exists
    assert '<source id="1">' in xml
    assert (
        "<page_summary>This is a brief overview of the article.</page_summary>" in xml
    )
    assert "Content chunk 1. [...] Content chunk 2." in xml

    # Source 2: Check Page Summary does NOT exist
    source_2_block = xml.split('<source id="2">')[1].split("</source>")[0]
    assert "<summary>" not in source_2_block
    assert "Only text here." in source_2_block

    # Instructions Check
    assert "<instructions>" in xml
    assert "</search_results>" in xml


@pytest.mark.asyncio
async def test_enhance_request_complete_success_flow() -> None:
    """
    Targets: enhance_request_with_web_context (Success path)
    Verifies the full loop from query extraction to XML injection.
    """
    manager = WebManager()

    # Setup Mock Provider
    mock_provider = AsyncMock()
    mock_provider.provider_name = "TestProvider"
    mock_provider.retrieve_context.return_value = SearchResult(
        query="bitcoin",
        webpages=[
            WebPage(url="https://btc.com", title="BTC", chunks=["Price is high"])
        ],
        time_ms={"total": 50, "search": 25},
    )

    # Mock the provider getter
    with patch.object(manager, "get_rag_provider", return_value=mock_provider):
        original_body = b'{"messages": [{"role": "user", "content": "What is the price of bitcoin?"}]}'

        result = await manager.enhance_request_with_web_context(original_body)

        assert result["websearchcontext"].executed is True
        assert "1" in result["websearchcontext"].sources
        assert result["websearchcontext"].sources["1"] == "https://btc.com"
        assert b"<search_results>" in result["body"]
        assert b"Price is high" in result["body"]


@pytest.mark.asyncio
async def test_enhance_request_outer_exception_handling() -> None:
    """
    Targets: enhance_request_with_web_context (Exception path)
    Ensures that if an API fails, the core doesn't crash and returns original body.
    """
    manager = WebManager()
    mock_provider = AsyncMock()
    # Simulate a crash in the provider
    mock_provider.retrieve_context.side_effect = Exception("API Timeout")

    with patch.object(manager, "get_rag_provider", return_value=mock_provider):
        body = b'{"messages": [{"role": "user", "content": "test"}]}'
        result = await manager.enhance_request_with_web_context(body)

        # Should catch the error and return success=False with the original body
        assert result["websearchcontext"].executed is False
        assert result["body"] == body


@pytest.mark.asyncio
async def test_inject_context_before_last_user_message() -> None:
    """
    Targets: inject_web_context_into_request
    Verifies that the system message is correctly placed BEFORE the last user message.
    """
    manager = WebManager()
    search_res = SearchResult(
        query="q", webpages=[WebPage(url="u", title="t", chunks=["c"])]
    )

    # Conversation: System -> User 1 -> Assistant -> User 2
    body_dict = {
        "messages": [
            {"role": "system", "content": "S1"},
            {"role": "user", "content": "U1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "U2"},
        ]
    }
    body_bytes = json.dumps(body_dict).encode("utf-8")

    enhanced_bytes, sources = await manager.inject_web_context_into_request(
        body_bytes, search_res, "q"
    )
    enhanced_dict = json.loads(enhanced_bytes)

    # Order should now be: S1 -> U1 -> A1 -> [NEW RAG SYSTEM MESSAGE] -> U2
    assert enhanced_dict["messages"][-1]["content"] == "U2"
    assert enhanced_dict["messages"][-2]["role"] == "system"
    assert "<search_results>" in enhanced_dict["messages"][-2]["content"]
    assert len(enhanced_dict["messages"]) == 5


@pytest.mark.asyncio
async def test_inject_context_json_decode_error() -> None:
    """
    Targets: inject_web_context_into_request (JSON Error path)
    Tests the exception handler when the request body is not valid JSON.
    """
    manager = WebManager()
    search_res = SearchResult(query="q", webpages=[WebPage(url="u")])

    bad_body = b"not a json string"

    # Should hit: except json.JSONDecodeError as e:
    body, sources = await manager.inject_web_context_into_request(
        bad_body, search_res, "q"
    )

    assert body == bad_body  # Returns original input on failure
    assert sources == {}


@pytest.mark.asyncio
async def test_inject_context_general_exception() -> None:
    """
    Targets: inject_web_context_into_request (General Exception path)
    Forces a TypeError (e.g. passing None) to check the general error catch.
    """
    manager = WebManager()
    # Passing None to an operation expecting bytes will trigger the general Exception block
    body, sources = await manager.inject_web_context_into_request(
        cast(bytes, None), cast(SearchResult, None), "q"
    )

    assert body is None
    assert sources == {}


@pytest.mark.asyncio
async def test_web_manager_search_factory() -> None:
    """Targets: get_web_search_provider (Serper and Fallbacks)"""
    manager = WebManager()

    # Test Serper
    settings.web_search_provider = "serper"
    settings.serper_api_key = "test_key"
    assert isinstance(await manager.get_web_search_provider(), SerperWebSearcher)

    # Test Fallback for unknown
    manager._search_provider = None
    settings.web_search_provider = "unsupported_search"
    assert await manager.get_web_search_provider() is None


@pytest.mark.asyncio
async def test_web_manager_scraper_factory() -> None:
    """Targets: get_web_scraper_provider (HTTP and Fallbacks)"""
    manager = WebManager()

    # Test Default/None
    assert isinstance(await manager.get_web_scraper_provider(), HTTPWebScraper)

    # Test Unknown strings fallback to HTTP
    manager._scraper_provider = None
    settings.web_scraper_provider = "mystery_scraper"
    assert isinstance(await manager.get_web_scraper_provider(), HTTPWebScraper)


@pytest.mark.asyncio
async def test_web_manager_chunker_factory() -> None:
    """Targets: get_web_chunker_provider (Recursive, Fixed, Fallbacks)"""
    manager = WebManager()

    # Test Recursive
    settings.web_chunker_provider = "recursive"
    assert isinstance(await manager.get_web_chunker_provider(), RecursiveChunker)

    # Test Fixed
    manager._chunker_provider = None
    settings.web_chunker_provider = "fixed"
    assert isinstance(await manager.get_web_chunker_provider(), FixedSizeChunker)

    # Test Invalid fallbacks to Recursive
    manager._chunker_provider = None
    settings.web_chunker_provider = "invalid"
    assert isinstance(await manager.get_web_chunker_provider(), RecursiveChunker)


@pytest.mark.asyncio
async def test_web_manager_ranker_factory() -> None:
    """Targets: get_web_ranker_provider (BM25 and Fallbacks)"""
    manager = WebManager()

    # Test BM25
    settings.web_ranking_provider = "bm25"
    assert isinstance(await manager.get_web_ranker_provider(), BM25Ranker)

    # Test fallbacks to BM25
    manager._rank_provider = None
    settings.web_ranking_provider = "invalid"
    assert isinstance(await manager.get_web_ranker_provider(), BM25Ranker)


@pytest.mark.asyncio
async def test_web_manager_rag_all_in_one_factories() -> None:
    """Targets: get_rag_provider (Tavily, Exa, Disabled)"""
    manager = WebManager()

    # Test Tavily
    settings.web_rag_provider = "tavily"
    settings.tavily_api_key = "test"
    with patch.object(
        TavilyWebRAG, "check_availability", new_callable=AsyncMock
    ) as mock:
        mock.return_value = True
        assert isinstance(await manager.get_rag_provider(), TavilyWebRAG)

    # Test Exa
    manager._rag_provider = None
    settings.web_rag_provider = "exa"
    settings.exa_api_key = "test"
    with patch.object(ExaWebRAG, "check_availability", new_callable=AsyncMock) as mock:
        mock.return_value = True
        assert isinstance(await manager.get_rag_provider(), ExaWebRAG)

    # Test Disabled
    manager._rag_provider = None
    settings.web_rag_provider = "disabled"
    assert await manager.get_rag_provider() is None


@pytest.mark.asyncio
async def test_web_manager_custom_rag_factory_success() -> None:
    """Targets: get_rag_provider (Custom RAG branch)"""
    manager = WebManager()
    settings.web_rag_provider = "custom"

    # Setup sub-components
    settings.web_search_provider = "serper"
    settings.serper_api_key = "test"
    settings.web_scraper_provider = "http"
    settings.web_chunker_provider = "recursive"
    settings.web_ranking_provider = "bm25"

    with patch.object(
        CustomWebRAG, "check_availability", new_callable=AsyncMock
    ) as mock:
        mock.return_value = True
        provider = await manager.get_rag_provider()
        assert isinstance(provider, CustomWebRAG)
