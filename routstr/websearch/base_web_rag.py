"""
Base classes for Retrieval Augmented Generation (RAG) providers.

This module defines the core interfaces and data structures for all-in-one RAG providers
that combine web search, content extraction, and chunking into a unified solution for
AI context enhancement.
"""

import json
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, Optional


from ..core.logging import get_logger
from .types import SearchResult

logger = get_logger(__name__)


class BaseWebRAG(ABC):
    """Base class for RAG providers.

    Defines the interface for providers that handle the complete RAG pipeline in single unified API call.
    """

    provider_name: str = "Base"

    def __init__(self) -> None:
        # Domain Blocklist will be passed to RAG provider and used if domain exclusion is supported
        self.EXCLUDE_DOMAINS = {
            "youtube.com",
            "youtu.be",
            "vimeo.com",
            "tiktok.com",
            "instagram.com",
            "facebook.com",
        }

    @abstractmethod
    async def retrieve_context(self, query: str, max_results: int = 5) -> SearchResult:
        """Perform web retrieval

        Args:
            query: The search query for retrieving relevant web content
            max_results: Maximum number of web sources to process

        Returns:
            SearchResult with processed content, chunks, and metadata
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

    async def _load_mock_data(self, file_name: str) -> Dict[str, Any]:
        """Load mock API response data from local JSON file for testing purposes.

        Args:
            file_name: Name of the JSON file containing mock response data

        Returns:
            Dictionary containing mock API response data

        """
        logger.debug("Using mock data from file.")
        from pathlib import Path

        script_dir = Path(__file__).parent
        json_file_path = script_dir / f"api_responses/{file_name}"
        with open(json_file_path, "r", encoding="utf-8") as file:
            return json.load(file)

    async def _save_api_response(
        self, response_data: Dict[str, Any], query: str, provider: str
    ) -> None:
        """Save live API response to timestamped JSON file for debugging and testing.

        Args:
            response_data: The API response dictionary to save
            query: The search query (used in filename generation)
            provider: Provider name (used in filename generation)

        Note:
            Creates files in api_responses/ directory with timestamp and sanitized query
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
        )[:60]
        filename = f"{provider}_{safe_query}_{timestamp}.json"
        file_path = responses_dir / filename

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(response_data, f, indent=2, ensure_ascii=False)
            logger.info(f"API response saved to {file_path}")
        except Exception as e:
            logger.error(f"Failed to save API response: {e}")
