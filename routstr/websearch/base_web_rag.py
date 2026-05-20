"""
Base classes for Retrieval Augmented Generation (RAG) providers.

This module defines the core interfaces and data structures for RAG modules
that combine web search, content extraction, and chunking into a unified solution for
AI context enhancement.
"""

from abc import ABC, abstractmethod

from ..core.logging import get_logger
from ..core.settings import settings
from .types import SearchResult

logger = get_logger(__name__)


class BaseWebRAG(ABC):
    """Base class for RAG providers.

    Defines the interface for providers that handle the complete RAG pipeline in single unified API call.
    """

    provider_name: str = "Base"

    def __init__(self) -> None:
        # Domain Blocklist will be passed to RAG provider and used if domain exclusion is supported
        self.EXCLUDED_DOMAINS = set(settings.web_excluded_domains)

    @abstractmethod
    async def retrieve_context(self, query: str, max_results: int = 5) -> SearchResult:
        """
        Perform complete RAG retrieval pipeline.

        This method is the primary entry point for all-in-one RAG providers.
        It handles search, extraction, and chunking in a single operation.

        Args:
            query: The search query for retrieving relevant web content.
            max_results: Maximum number of web sources to process.

        Returns:
            SearchResult with processed content, chunks, and metadata.
        """

    @abstractmethod
    async def check_availability(self) -> bool:
        """Check if the RAG provider service is available and API key is valid.

        Performs a lightweight API call to verify:
        - Service accessibility
        - API key validity
        - Service operational status

        Returns:
            True if provider is available and functional, False otherwise
        """
