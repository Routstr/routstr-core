"""Abstract base class for search result ranking and pruning of low-ranking chunks."""

from abc import ABC, abstractmethod

from .types import SearchResult


class BaseRanker(ABC):
    """
    Base class for search result ranking and pruning.

    Rankers are responsible for evaluating the relevance of retrieved chunks
    against the original query and selecting the most relevant ones for
    inclusion in the LLM context.
    """

    def __init__(
        self, provider_name: str = "base", max_chunks_per_source: int = 5
    ) -> None:
        self.provider_name = provider_name
        self.max_chunks_per_source = max_chunks_per_source

    @abstractmethod
    async def rank(self, search_result: SearchResult, query: str) -> SearchResult:
        """
        Rank and prune chunks within a SearchResult.

        Evaluates chunks based on relevance to the query and limits the number
        of chunks per source according to max_chunks_per_source.

        Args:
            search_result: The SearchResult containing chunked pages.
            query: The original search query for relevance scoring.

        Returns:
            A new SearchResult with ranked and filtered chunks.
        """
        pass

    @abstractmethod
    async def check_availability(self) -> bool:
        """Verify the ranker is functional."""
        pass
