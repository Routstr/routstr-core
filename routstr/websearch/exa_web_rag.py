"""
Exa API RAG Provider Module

This module provides a complete RAG implementation using the Exa API.
Exa delivers intelligent web search with embeddings-based ranking, content extraction,
and highlight generation optimized for AI context enhancement.
"""

import json
from datetime import datetime, timezone
from typing import Any

from ..core.logging import get_logger
from ..core.settings import settings
from .base_web_rag import BaseWebRAG
from .http_client import HTTPClient
from .types import SearchResult, WebPage

logger = get_logger(__name__)

# Currently unused import for mock websearch
# from .mock_utils import _save_api_response


class ExaWebRAG(BaseWebRAG):
    """
    All-in-one RAG provider using the Exa API for neural web search and content extraction.
    """

    provider_name = "Exa"

    def __init__(self, api_key: str):
        """
        Initialize the Exa RAG provider.

        Args:
            api_key: The Exa API key for authentication

        Raises:
            ValueError: If API key is empty or None
        """
        super().__init__()

        if not api_key:
            raise ValueError("Exa API key cannot be empty.")

        self.client = HTTPClient(
            base_url="https://api.exa.ai", default_headers={"x-api-key": api_key}
        )

        self.api_key = api_key

        logger.info("ExaWebRAG initialized.")

    async def retrieve_context(self, query: str, max_results: int = 10) -> SearchResult:
        """
        Perform RAG retrieval using Exa's neural search API.

        Uses neural search to find semantically relevant content. Retrieves
        highlights (most relevant sentences) for each result to provide
        concise context for the LLM.

        Args:
            query: The search query for retrieving relevant web content.
            max_results: Maximum number of web sources to process.

        Returns:
            SearchResult with extracted highlights.
        """
        start_time = datetime.now()
        logger.info(
            f"Performing Exa API search for: '{query}'",
            extra={"query": query, "max_results": max_results},
        )

        try:
            # --- MOCK DATA FOR TESTING ---
            # api_response = await _load_mock_data(
            #    "exa_What_happend_between_the_US_and_Venezuela_20260116_122619.json"
            # "exa_what_is_the_latest_news_about_the_Donald_Trump_peace_deal_Which_websites_did_you_search_be_brief_20251219_163302.json"
            # exa_what_is_the_state_of_the_US_jobmarket_currently_Which_websites_did_you_search_be_brief_20251223_145745.json
            # )
            # ---------------------
            api_response = await self._call_exa_api(query, max_results)
            # await _save_api_response(api_response, query, "exa")
            # ---------------------------------------------------------------

            total_ms = int((datetime.now() - start_time).total_seconds() * 1000)
            return self._map_to_search_result(api_response, query, total_ms)

        except json.JSONDecodeError as e:
            error_msg = f"Failed to parse Exa API response for query '{query}': {e}"
            logger.error(error_msg, extra={"query": query, "error": str(e)})
            raise Exception(error_msg)
        except Exception as e:
            error_msg = (
                f"Failed to get or process Exa API response for query '{query}': {e}"
            )
            logger.error(error_msg, extra={"query": query, "error": str(e)})
            raise Exception(error_msg)

    def _map_to_search_result(
        self, api_response: dict[str, Any], query: str, total_ms: int
    ) -> SearchResult:
        """
        Map Exa API response to a SearchResult object.

        Args:
            api_response: The raw response from Exa API
            query: The original search query

        Returns:
            A populated SearchResult object
        """
        exa_results = api_response.get("results", [])
        parsed_results = []

        for i, web_page in enumerate(exa_results):
            # Use highlights as list of strings
            highlights = web_page.get("highlights", None)

            result = WebPage(
                title=web_page.get("title", None),
                url=web_page.get("url", "Unknown URL"),
                summary=web_page.get("summary", None),
                publication_date=web_page.get("publishedDate", None),
                relevance_score=web_page.get(
                    "score", 1.0 - (i * 0.1)
                ),  # Fallback assumes results in order of relevance
                content=web_page.get("text", None),
                chunks=highlights,
            )
            parsed_results.append(result)

        if not parsed_results:
            logger.warning(f"No results found for query: '{query}'")

        logger.info(
            f"Exa search completed successfully: {len(parsed_results)} results",
            extra={
                "query": query,
                "result_count": len(parsed_results),
                "total_ms": total_ms,
            },
        )

        return SearchResult(
            query=query,
            webpages=parsed_results,
            summary=None,
            time_ms={"total": total_ms},
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    async def _call_exa_api(self, query: str, max_results: int = 10) -> dict[str, Any]:
        """
        Make live API call to Exa's neural search endpoint.

        Configures the payload for 'neural' search with highlight extraction.
        Disables raw text and summary retrieval to focus on high-relevance chunks.

        Args:
            query: The search query.
            max_results: Maximum number of results to return (max 10).

        Returns:
            Dictionary containing the complete Exa API response.
        """
        logger.debug(f"Making live Exa API call for: '{query}'")

        # Exa request payload configured for neural search with context
        payload = {
            "query": query,
            "type": "neural",
            "numResults": min(max_results, 100),  # Exa's max is 100
            "contents": {
                "text": False,  # Complete page content
                "summary": False,  # Short summary of page content
                "highlights": {  # Most relevant chunks
                    "numSentences": 3,  # Number of sentences per highlight
                    "highlightsPerUrl": settings.max_chunks_per_source,  # Chunks per URL
                },
                "livecrawl": "preferred",  # https://exa.ai/docs/reference/livecrawling-contents
                "extras": {"links": 0, "imageLinks": 0},
                "subpageTarget": None,
                "subpages": 0,  # Do not scrape subpages
            },
            "context": False,  # Enable context to get combined content for LLMs
            "includeText": None,
            "excludeText": None,
            "startCrawlDate": None,
            "endCrawlDate": None,
            "startPublishedDate": None,
            "endPublishedDate": None,
            "includeDomains": None,
            "excludeDomains": list(self.EXCLUDED_DOMAINS)
            if self.EXCLUDED_DOMAINS
            else [],
            "category": None,
        }

        # Make the API request
        return await self.client.post(url="/search", json_data=payload)

    async def check_availability(
        self,
    ) -> bool:
        """
        Verify Exa service availability.

        Makes a lightweight call to Exa's health endpoint to confirm:
        - Service is accessible and operational

        Exa's /health endpoint returns the string "I am healthy." if healthy

        Returns:
            True if Exa service is available, False otherwise
        """
        logger.debug("Checking Exa API availability")

        try:
            await self.client.get(url="/health", return_json=False)
            return True
        except Exception as e:
            logger.error(f"Exa availability check failed: {e}")
            return False
