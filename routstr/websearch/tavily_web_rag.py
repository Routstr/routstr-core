"""
Tavily API RAG Provider Module

This module provides a complete RAG implementation using the Tavily API.
Tavily offers an all-in-one solution combining web search, content extraction,
and intelligent chunking specifically designed for AI context enhancement.
"""

import json
from datetime import datetime, timezone
from typing import Any

from ..core.logging import get_logger
from .base_web_rag import BaseWebRAG
from .http_client import HTTPClient
from .types import SearchResult, WebPage
from ..core.settings import settings


logger = get_logger(__name__)


class TavilyWebRAG(BaseWebRAG):
    """
    All-in-one RAG provider using the Tavily API for intelligent web content retrieval.

    Tavily handles the complete pipeline: web search, content extraction,
    relevance ranking, and chunking in a single API call, making it ideal
    for Retrieval Augmented Generation use cases.
    """

    provider_name = "Tavily"

    def __init__(self, api_key: str):
        """
        Initialize the Tavily RAG provider.

        Args:
            api_key: The Tavily API key for authentication

        Raises:
            ValueError: If API key  is empty or None
        """
        super().__init__()

        if not api_key:
            raise ValueError("Tavily API key cannot be empty.")

        self.client = HTTPClient(
            base_url="https://api.tavily.com",
            default_headers={"Authorization": f"Bearer {api_key}"},
        )

        self.api_key = api_key

        logger.info("TavilyWebRAG initialized.")

    async def retrieve_context(self, query: str, max_results: int = 10) -> SearchResult:
        """
        Perform complete RAG pipeline using Tavily's all-in-one API.

        Executes web search, content extraction, and chunking in a single call.
        Queries exceeding 400 characters are truncated to meet API limits.
        Uses 'advanced' search depth to retrieve RAG-optimized content chunks.

        Args:
            query: The search query for retrieving relevant web content.
            max_results: Maximum number of web sources to process (max 20).

        Returns:
            SearchResult with processed content, pre-chunked highlights, and metadata.
        """
        start_time = datetime.now()

        if len(query) >= 400:
            query = query[:400]
            logger.warning(
                f"Tavily's limit of 400 characters exceeded with {len(query)} characters. Using only first 400 characters."
            )

        logger.info(
            f"Performing Tavily API search for: '{query}'",
            extra={"query": query, "max_results": max_results},
        )

        try:
            # --- MOCK DATA FOR TESTING  ---
            api_response = await self._load_mock_data(
                "tavily_What_are_the_current_developments_between_the_USA_and_Greenl_20260118_122001.json"
                # tavily_what_is_the_state_of_the_US_jobmarket_currently_Which_websites_did_you_search_be_brief_20251223_150031.json
            )
            # ---------------------------------------------------------------
            # api_response = await self._call_tavily_api(query, max_results)
            # await self._save_api_response(api_response, query, "tavily")
            # ---------------------------------------------------------------

            total_ms = int((datetime.now() - start_time).total_seconds() * 1000)

            return self._map_to_search_result(api_response, query, total_ms)

        except json.JSONDecodeError as e:
            error_msg = f"Failed to parse Tavily API response for query '{query}': {e}"
            logger.error(error_msg, extra={"query": query, "error": str(e)})
            raise Exception(error_msg)
        except Exception as e:
            error_msg = (
                f"Failed to get or process Tavily API response for query '{query}': {e}"
            )
            logger.error(error_msg, extra={"query": query, "error": str(e)})
            raise Exception(error_msg)

    def _map_to_search_result(
        self, api_response: dict[str, Any], query: str, total_ms: int
    ) -> SearchResult:
        """
        Map Tavily API response to a SearchResult object.

        Extracts results and maps them to WebPage objects. Tavily's 'content'
        field is split into chunks using the ' [...] ' delimiter.

        Args:
            api_response: The raw response from Tavily API.
            query: The original search query.
            total_ms: Total execution time in milliseconds.

        Returns:
            A populated SearchResult object.
        """
        tavily_results = api_response.get("results", [])
        parsed_results = []

        for i, web_page in enumerate(tavily_results):
            result = WebPage(
                title=web_page.get("title", None),
                url=web_page.get("url", "Unknown URL"),
                summary=None,  # Summary not supported by tavily
                publication_date=None,  # Tavily doesn't provide publish date
                relevance_score=web_page.get(
                    "score", 1.0 - (i * 0.1)
                ),  # Fallback assumes results in order of relevance
                content=web_page.get(
                    "raw_content", None
                ),  # Complete webpage content (usually unused)
                chunks=web_page.get("content", "").split(" [...] ")
                if web_page.get("content")
                else None,  # Tavily's pre-chunked content
            )
            parsed_results.append(result)

        if not parsed_results:
            logger.warning(f"No results found for query: '{query}'")

        logger.info(
            f"Tavily search completed successfully: {len(parsed_results)} results",
            extra={
                "query": query,
                "result_count": len(parsed_results),
                "total_ms": total_ms,
            },
        )

        return SearchResult(
            query=query,
            webpages=parsed_results,
            summary=api_response.get("answer", None),
            time_ms={"total": total_ms},
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    async def _call_tavily_api(
        self, query: str, max_results: int = 10
    ) -> dict[str, Any]:
        """
        Make live API call to Tavily's advanced search endpoint.

        Args:
            query: The search query
            max_results: Maximum number of results to return (max 10)

        Returns:
            Dictionary containing the complete Tavily API response

        Raises:
            Exception: If API call fails or returns non-200 status

        Note:
            Uses advanced search depth with chunking enabled for optimal RAG performance
        """
        logger.debug(f"Making live Tavily API call for: '{query}'")

        # Tavily request payload with all-in-one RAG parameters
        payload = {
            "query": query,
            "topic": "general",  # Options: general, news, finance
            "auto_parameters": False,
            "country": None,
            "search_depth": "advanced",  # Use advanced to get chunks functionality
            "include_answer": True,  # Includes a LLM summary when set True -> no further cost but latency
            "include_images": False,  # We don't need images for RAG
            "include_favicon": False,
            "include_raw_content": False,  # We'll use chunks instead of raw content
            "max_results": min(max_results, 20),  # Tavily max is 20 (2026-01-13)
            "exclude_domains": list(self.EXCLUDED_DOMAINS)
            if self.EXCLUDED_DOMAINS
            else [],
            "time_range": None,  # No filtering by publishing date
            "start_date": None,
            "end_date": None,
            "chunks_per_source": min(3, settings.max_chunks_per_source),  # Tavily's max: 3
            "include_usage": True,  # Can be used to update admin interface in the future
        }

        # Make the API request
        return await self.client.post(url="/search", json_data=payload)

    async def check_availability(
        self,
    ) -> bool:
        """
        Verify Tavily service availability.

        Makes a lightweight call to Tavily's usage endpoint to confirm:
        - Service is accessible and operational

        Returns:
            True if Tavily service is available and API key is valid, False otherwise
        """
        logger.debug("Checking Tavily API availability")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
        }

        try:
            await self.client.get(
                url="/usage",
                headers=headers,
            )
            return True
        except Exception as e:
            logger.error(f"Tavily availability check failed: {e}")
            return False
