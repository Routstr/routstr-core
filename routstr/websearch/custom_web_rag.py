"""
Custom RAG Provider Module

This module provides a manual RAG implementation that combines separate components:
web search, content scraping, and text chunking into a unified RAG pipeline.
Offers flexibility to use different providers for each pipeline stage.
"""

from dataclasses import replace
from datetime import datetime, timezone
from typing import Dict

from ..core.logging import get_logger
from .base_chunker import BaseChunker
from .base_web_rag import BaseWebRAG
from .base_ranker import BaseRanker
from .base_web_scraper import BaseWebScraper
from .base_web_searcher import BaseWebSearcher
from .types import SearchResult

logger = get_logger(__name__)


class CustomRAG(BaseWebRAG):
    """
    Manual RAG pipeline implementation using separate search, scraping, and chunking components.

    Combines any web search provider, web scraper, and chunker to provide a complete
    RAG solution. Offers maximum flexibility for customizing each pipeline stage.
    """

    provider_name = "Custom"

    def __init__(
        self,
        search_provider: BaseWebSearcher,
        scrape_provider: BaseWebScraper,
        chunk_provider: BaseChunker,
        rank_provider: BaseRanker,
    ):
        """
        Initialize CustomRAG with pipeline components.

        Args:
            search_provider: Web search provider (e.g., SerperWebSearch)
            scraper_provider: Web scraper for content extraction
            chunker_provider: Text chunker for content processing

        Raises:
            ValueError: If any provider is None
        """
        if not search_provider:
            raise ValueError("Search provider cannot be None")
        if not scrape_provider:
            raise ValueError("Scraper provider cannot be None")
        if not chunk_provider:
            raise ValueError("Chunker provider cannot be None")
        if not rank_provider:
            raise ValueError("Ranker provider cannot be None")

        self.search_provider = search_provider
        self.scraper_provider = scrape_provider
        self.chunker_provider = chunk_provider
        self.rank_provider = rank_provider

        logger.info(
            f"CustomRAG initialized with: {search_provider.__class__.__name__}, "
            f"{scrape_provider.__class__.__name__}, {chunk_provider.__class__.__name__}, "
            f"{rank_provider.__class__.__name__}"
        )

    async def retrieve_context(self, query: str, max_results: int = 10) -> SearchResult:
        """
        Execute custom configurable RAG pipeline.

        Performs the full pipeline:
        1. Web search
        2. Content scraping
        3. Chunking
        4. Ranking (keeps only relevant chunks)

        Args:
            query: The search query for retrieving relevant web content
            max_results: Maximum number of web sources to process

        Returns:
            SearchResult

        Raises:
            Exception: If any pipeline stage fails
        """
        pipeline_start = datetime.now()
        timings: Dict[str, int] = {}
        logger.info(f"Starting CustomRAG pipeline for query: '{query}'")

        try:
            # Search
            stage_start = datetime.now()
            search_response = await self.search_provider.search(query, max_results)
            timings["search"] = int((datetime.now() - stage_start).total_seconds() * 1000)


            if not search_response.webpages:
                logger.warning(f"No search results found for query: '{query}'")
                return SearchResult(
                    query=query,
                    webpages=[],
                    summary=None,
                    time_ms=timings,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
            
            # SCRAPE
            stage_start = datetime.now()
            search_response = await self.scraper_provider.scrape_search_results(
                search_response
            )
            timings["scrape"] = int((datetime.now() - stage_start).total_seconds() * 1000)

            # 3. CHUNK
            step_start = datetime.now()
            search_response = await self.chunker_provider.chunk_search_results(search_response)
            timings["chunk"] = int((datetime.now() - step_start).total_seconds() * 1000)

            step_start = datetime.now()
            search_response = await self.rank_provider.rank(search_response, query)
            timings["rank"] = int((datetime.now() - step_start).total_seconds() * 1000)

            # Calculate total pipeline time
            timings["total"] = int((datetime.now() - pipeline_start).total_seconds() * 1000)

            logger.info(
                f"CustomRAG pipeline completed: {len(search_response.webpages)} results in {timings['total']}ms",
                extra={"timing (ms)": timings}
            )

            # Update timing metadata and return a new object
            return replace(
                search_response,
                time_ms=timings,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

        except Exception as e:
            error_msg = f"CustomRAG pipeline failed for query '{query}': {e}"
            logger.error(error_msg)
            raise Exception(error_msg)

    async def check_availability(self) -> bool:
        """
        Verify all pipeline components are available and functional.

        Checks availability of all components

        Returns:
            True if all components are available, False otherwise
        """
        try:
            # Check search provider availability
            search_available = await self.search_provider.check_availability()

            # Check scraper availability
            scraper_available = await self.scraper_provider.check_availability()

            # Check chunker availability #TODO make consistant to scraper and search
            chunker_available = self.chunker_provider.validate_parameters()

            all_available = search_available and scraper_available and chunker_available

            if not all_available:
                logger.warning(
                    f"CustomRAG availability check failed: "
                    f"search={search_available}, scraper={scraper_available}, chunker={chunker_available}"
                )
            else:
                logger.debug("CustomRAG all components available")

            return all_available

        except Exception as e:
            logger.error(f"CustomRAG availability check failed: {e}")
            return False
