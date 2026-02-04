"""
Base chunker module for text content granulation.

This module provides the abstract base class for text chunking algorithms,
following the same modular pattern as the web search and scraping components.
"""

import asyncio
from abc import ABC, abstractmethod
from dataclasses import replace
from typing import List

from ..core.logging import get_logger
from .types import SearchResult, WebPage

logger = get_logger(__name__)


class BaseChunker(ABC):
    """
    Base class for content chunkers.

    Chunkers are responsible for breaking down large text content into smaller,
    manageable pieces (chunks) that can be effectively processed by LLMs or
    ranked for relevance.
    """

    chunker_name: str = "base"

    def __init__(self, chunk_size: int = 500, chunk_overlap: int = 0) -> None:
        """
        Initialize the chunker.

        Args:
            chunk_size: Maximum size of each chunk in characters
            chunk_overlap: Number of characters to overlap between chunks
        """
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        logger.info(
            f"Initialized {self.chunker_name} chunker with size={chunk_size}, overlap={chunk_overlap}"
        )

    @abstractmethod
    async def chunk_text(self, text: str) -> List[str]:
        """
        Chunk text into smaller pieces and return list of strings.

        Args:
            text: The text to chunk
            **kwargs: Additional parameters for chunking algorithms

        Returns:
            List of text chunks as strings
        """
        raise NotImplementedError("Subclasses must implement chunk_text method")

    async def chunk_search_results(self, search_result: SearchResult) -> SearchResult:
        """
        Chunk the content in search results concurrently.

        Iterates through all webpages in the SearchResult and applies the
        chunking algorithm to their text content.

        Args:
            search_result: The SearchResult containing scraped webpages.

        Returns:
            A new SearchResult with 'chunks' populated for each webpage.
        """
        if not search_result.webpages:
            return search_result
        logger.info(
            f"Chunking content for {len(search_result.webpages)} search results concurrently"
        )

        async def process(result: WebPage) -> WebPage:
            if not result.content:
                return result
            try:
                chunks = await self.chunk_text(result.content)

                return replace(result, chunks=chunks)
            except Exception as e:
                logger.error(f"Failed to chunk content for {result.url}: {e}")
                return replace(result, chunks=None)

        tasks = [process(result) for result in search_result.webpages]

        if tasks:
            chunked_webpages = await asyncio.gather(*tasks)
            return replace(search_result, webpages=list(chunked_webpages))

        return search_result

    def validate_parameters(self) -> bool:
        """
        Validate chunker parameters.

        Returns:
            True if parameters are valid, False otherwise
        """
        if self.chunk_size <= 0:
            logger.error(f"Invalid chunk_size: {self.chunk_size}. Must be > 0")
            return False

        if self.chunk_overlap < 0:
            logger.error(f"Invalid chunk_overlap: {self.chunk_overlap}. Must be >= 0")
            return False

        if self.chunk_overlap >= self.chunk_size:
            logger.error(
                f"chunk_overlap ({self.chunk_overlap}) must be less than chunk_size ({self.chunk_size})"
            )
            return False

        return True
