"""
Base class for Web search for AI context enhancement using Retrieval Augmented Generation (RAG).

"""

import json
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from ..core.logging import get_logger
from ..core.settings import settings
from .types import SearchResult

logger = get_logger(__name__)


class BaseWebSearcher(ABC):
    """Base class for web search providers."""

    provider_name: str = "Base"

    def __init__(self) -> None:
        """Initialize the base web search provider."""

        # Domain Blocklist will be used by Search provider if domain exclusion is supported
        self.EXCLUDED_DOMAINS = set(settings.web_excluded_domains)

    @abstractmethod
    async def search(self, query: str, max_results: int = 5) -> SearchResult:
        """
        Performs web search and return results.

        Args:
            query: The search query.
            max_results: Maximum number of results to return.

        Returns:
            SearchResult containing the list of found webpages.
        """

    @abstractmethod
    async def check_availability(self) -> bool:
        """
        Check if the search provider API is healthy and reachable.
        Returns: True if available, False otherwise.
        """

    def is_excluded(self, url: str) -> bool:
        """
        Check if a URL's domain is in the blocklist.

        Args:
            url: The URL to check.

        Returns:
            True if the domain is blocked, False otherwise.
        """
        # Extract domain from URL
        domain = urlparse(url).netloc
        # Remove 'www.' if present to ensure matching
        if domain.startswith("www."):
            domain = domain[4:]
        return domain in self.EXCLUDED_DOMAINS
