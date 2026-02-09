import pytest

from routstr.websearch.bm25_ranker import BM25Ranker
from routstr.websearch.types import SearchResult, WebPage


@pytest.mark.asyncio
async def test_bm25_tokenization_logic() -> None:
    """Verify that tokenization handles case sensitivity and punctuation."""
    # We test the internal _sort_chunks logic
    ranker = BM25Ranker(max_chunks_per_source=5)
    query = "->bitcoin?!"

    chunks = [
        "The weather is nice and the sun is shining today.",  # Irrelevant
        "Bitcoin is a decentralized digital currency.",  # RELEVANT
        "This chunk is about cats and dogs in the park.",  # Irrelevant
    ]
    sorted_chunks = ranker._sort_chunks(query, chunks)

    assert sorted_chunks[0] == "Bitcoin is a decentralized digital currency."
    assert len(sorted_chunks) == 3


@pytest.mark.asyncio
async def test_bm25_local_pruning() -> None:
    """Verify that each webpage is limited to local_k chunks."""
    local_limit = 2
    ranker = BM25Ranker(max_chunks_per_source=local_limit)

    # Mock a page with 5 chunks
    page = WebPage(
        url="https://test.com", title="Test", chunks=["C1", "C2", "C3", "C4", "C5"]
    )
    mock_result = SearchResult(query="test", webpages=[page])

    # Act: Perform only the local pruning step
    pruned_result = ranker._rank_local(mock_result, "test", top_k=local_limit)

    # Assert
    assert len(pruned_result.webpages[0].chunks) == local_limit


@pytest.mark.asyncio
async def test_bm25_global_pruning() -> None:
    """Verify that the total number of chunks across all pages is capped at global_k."""
    ranker = BM25Ranker(max_chunks_per_source=10)
    ranker.global_k = 3  # Force a small global limit for testing

    # Two pages with 3 chunks each (total 6)
    page_a = WebPage(url="A", title="A", chunks=["A1", "A2", "A3"])
    page_b = WebPage(url="B", title="B", chunks=["B1", "B2", "B3"])

    mock_result = SearchResult(query="test", webpages=[page_a, page_b])

    # Act: Rank globally
    final_result = ranker._rank_global(mock_result, "test", total_k=ranker.global_k)

    # Count total surviving chunks
    total_chunks = sum(len(p.chunks) for p in final_result.webpages if p.chunks)

    assert total_chunks == 3


@pytest.mark.asyncio
async def test_bm25_relevance_ranking_accuracy() -> None:
    """Verify that the most relevant chunks are actually selected."""
    ranker = BM25Ranker(max_chunks_per_source=5)
    ranker.global_k = 1  # We only want the single best chunk

    query = "what is the lightning network?"

    page = WebPage(
        url="https://crypto.com",
        title="Crypto",
        chunks=[
            "The Lightning Network is a layer 2 scaling solution.",
            "I like to eat apples in the morning.",
            "Python is a programming language.",
        ],
    )

    mock_result = SearchResult(query=query, webpages=[page])

    # Full rank process
    final_result = await ranker.rank(mock_result, query)

    surviving_chunks = final_result.webpages[0].chunks
    assert len(surviving_chunks) == 1
    assert "The Lightning Network is a layer 2 scaling solution." == surviving_chunks[0]


@pytest.mark.asyncio
async def test_bm25_handles_empty_results() -> None:
    """Ensure the ranker doesn't crash if no pages or chunks are provided."""
    ranker = BM25Ranker()

    # Empty result
    empty_result = SearchResult(query="test", webpages=[])
    res = await ranker.rank(empty_result, "test")
    assert res.webpages == []

    # Page with None chunks
    page_none = WebPage(url="test", title="test", chunks=None)
    result_none = SearchResult(query="test", webpages=[page_none])
    res_none = await ranker.rank(result_none, "test")
    assert res_none.webpages[0].chunks is None
