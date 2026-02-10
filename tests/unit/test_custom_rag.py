from unittest.mock import AsyncMock, MagicMock

import pytest

from routstr.websearch.custom_web_rag import CustomRAG
from routstr.websearch.types import SearchResult, WebPage


@pytest.mark.asyncio
async def test_custom_rag_pipeline_flow() -> None:
    """
    Verify that CustomRAG calls each stage of the pipeline in the correct order
    and correctly aggregates the timing metadata.
    """
    # 1. Setup Mocks for all 4 stages
    mock_search = AsyncMock()
    mock_scrape = AsyncMock()
    mock_chunk = AsyncMock()
    mock_rank = AsyncMock()

    # Define a standard return flow
    initial_res = SearchResult(query="test", webpages=[WebPage(url="http://test.com")])
    mock_search.search.return_value = initial_res
    mock_scrape.scrape_search_results.return_value = initial_res
    mock_chunk.chunk_search_results.return_value = initial_res
    mock_rank.rank.return_value = initial_res

    pipeline = CustomRAG(
        search_provider=mock_search,
        scrape_provider=mock_scrape,
        chunk_provider=mock_chunk,
        rank_provider=mock_rank,
    )

    result = await pipeline.retrieve_context("test query", max_results=5)

    # Verify Pipeline Execution
    mock_search.search.assert_called_once()
    mock_scrape.scrape_search_results.assert_called_once()
    mock_chunk.chunk_search_results.assert_called_once()
    mock_rank.rank.assert_called_once()

    # Timing Metadata
    assert "search" in result.time_ms
    assert "scrape" in result.time_ms
    assert "chunk" in result.time_ms
    assert "rank" in result.time_ms
    assert "total" in result.time_ms
    assert result.time_ms["total"] >= 0


@pytest.mark.asyncio
async def test_custom_rag_stop_on_no_results() -> None:
    """Verify that if search returns no results, the pipeline stops early to save resources."""
    mock_search = AsyncMock()
    mock_scrape = AsyncMock()

    # Search returns empty list
    mock_search.search.return_value = SearchResult(query="test", webpages=[])

    pipeline = CustomRAG(
        search_provider=mock_search,
        scrape_provider=mock_scrape,
        chunk_provider=AsyncMock(),
        rank_provider=AsyncMock(),
    )

    result = await pipeline.retrieve_context("test query")

    assert len(result.webpages) == 0
    mock_search.search.assert_called_once()

    mock_scrape.scrape_search_results.assert_not_called()


@pytest.mark.asyncio
async def test_custom_rag_availability_check() -> None:
    """Verify that check_availability only returns True if ALL components are healthy."""
    mock_search = AsyncMock()
    mock_scrape = AsyncMock()
    mock_chunk = AsyncMock()  
    mock_rank = AsyncMock()  

    pipeline = CustomRAG(mock_search, mock_scrape, mock_chunk, mock_rank)

    # Case 1: Scrape is down
    mock_search.check_availability.return_value = True
    mock_scrape.check_availability.return_value = False
    mock_chunk.check_availability.return_value = True
    mock_rank.check_availability.return_value = True
    assert await pipeline.check_availability() is False

    # Case 2: All are up
    mock_scrape.check_availability.return_value = True
    assert await pipeline.check_availability() is True
