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

    async def _load_mock_data(self, file_name: str) -> dict[str, Any]:
        """
        Load mock data from local JSON file for testing purposes.

        Returns:
            Dictionary containing mock API response
        """
        logger.debug("Using mock data from file.")
        from pathlib import Path

        script_dir = Path(__file__).parent
        json_file_path = script_dir / f"api_responses/{file_name}"
        with open(json_file_path, "r", encoding="utf-8") as file:
            return json.load(file)

    async def _save_api_response(
        self, response_data: dict[str, Any], query: str, provider: str
    ) -> None:
        """
        Save API response to a timestamped JSON file.

        Args:
            response_data: The API response dictionary to save
            query: The search query (used in filename)
        """
        import json
        from pathlib import Path

        # Create responses directory if it doesn't exist
        responses_dir = Path(__file__).parent / "api_responses"
        responses_dir.mkdir(exist_ok=True)

        # Generate filename with timestamp and sanitized query
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_query = (
            "".join(c for c in query if c.isalnum() or c in (" ", "-", "_"))
            .rstrip()
            .replace(" ", "_")
        )
        filename = f"{provider}_{safe_query}_{timestamp}.json"
        file_path = responses_dir / filename

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(response_data, f, indent=2, ensure_ascii=False)
            logger.info(f"API response saved to {file_path}", extra={"path": str(file_path)})
        except Exception as e:
            logger.error(
                f"Failed to save API response: {e}",
                extra={"path": str(file_path), "error": str(e)},
            )
