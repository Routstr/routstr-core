"""
Serper API Searcher Module

This module provides a WebSearchProvider implementation that uses the Serper API
to get search results and formats them into the standard WebSearchResponse.
"""

from datetime import datetime, timezone
from typing import Any, Dict

from ..core.logging import get_logger
from .base_web_searcher import BaseWebSearcher
from .types import SearchResult, WebPage
from .http_client import HTTPClient

logger = get_logger(__name__)


class SerperWebSearcher(BaseWebSearcher):
    """
    A web search provider that uses the Serper API to get search results.
    """

    provider_name = "Serper"

    def __init__(self, api_key: str):
        """
        Initialize the Serper provider.

        Args:
            api_key: The Serper API key.
        """

        super().__init__()


        if not api_key:
            raise ValueError("Serper API key cannot be empty.")
        self.api_key = api_key

        
        self.client = HTTPClient(
            base_url="https://google.serper.dev",
            default_headers={"X-API-KEY": api_key}
        )

        logger.info("SerperWebSearch initialized with API key.")

    async def search(self, query: str, max_results: int = 10) -> SearchResult:
        """
        Perform web search using the Serper API and return a SearchResponse.
        """
        start_time = datetime.now()
        logger.info(f"Performing Serper API search for: '{query}'")

        try:
            # --- MOCK DATA FOR TESTING ---
            api_response = await self._load_mock_data(
                "serper_What_happend_between_the_US_and_Venezuela_20260115_103055.json"
                #    "serper_trump-peace-plan.json"
                #    serper_what_is_the_state_of_the_US_jobmarket_currently_Which_websites_did_you_search_be_brief_20251223_150343.json
            )
            # ---------------------------------------------------------------
            # api_response = await self._call_serper_api(query, max_results)
            # await self._save_api_response(api_response, query, "serper")
            # ---------------------------------------------------------------


            return self._map_to_search_result(api_response, query)

        except FileNotFoundError:
            error_msg = "Dummy data file not found."
            logger.error(error_msg)
            raise Exception(error_msg)
        except Exception as e:
            error_msg = (
                f"Failed to get or process Serper API response for query '{query}': {e}"
            )
            logger.error(error_msg)
            raise Exception(error_msg)

    async def _call_serper_api(self, query: str, max_results: int) -> Dict[str, Any]:
        return await self.client.post(
            url="/search",
            json_data={"q": query, "num": max_results}
        )
    

    def _map_to_search_result(
        self, api_response: Dict[str, Any], query: str
    ) -> SearchResult:
        """
        Map Serper API response to a SearchResult object.
        Args:
            api_response: The raw response from Serper API
            query: The original search query
        Returns:
            A populated SearchResult object
        """
        serper_results = api_response.get("organic", [])
        parsed_results = []

        for i, item in enumerate(serper_results):
            url = item.get("link", "Unknown URL")
            
            if not self.is_blocked(url=url):
                result = WebPage(
                    title=item.get("title", None),
                    url=url,
                    summary=item.get("snippet", None),
                    publication_date=item.get("date", None),
                    relevance_score=1.0 - (i * 0.1),  # Position-based relevance
                    content=None,           # Serper organic doesn't provide full content
                    chunks=None,    # Serper doesn't provide RAG chunks
                )
                parsed_results.append(result)

        if not parsed_results:
            logger.warning(f"No results found for query: '{query}'")

        logger.info(
            f"Serper search completed successfully: {len(parsed_results)} results"
        )

        return SearchResult(
            query=query,
            webpages=parsed_results,
            summary=None,  # Serper doesn't provide a generated answer in the organic endpoint
            timestamp=datetime.now(timezone.utc).isoformat(),
        )


    async def check_availability(self) -> bool:
        """
        Check if the Serper API is available.
        Returns: True if the API returns status 'ok', False otherwise.
        """
        logger.debug("Checking Serper API availability...")
        try:
            response = await self.client.get(url="/health")

            available = response.get("status") == "ok"
            if available:
                logger.info("Serper API health check passed.")
            else:
                logger.warning(
                    f"Serper API health check returned unexpected status: {response}"
                )

            return available

        except Exception as e:
            logger.error(f"Serper API availability check failed: {e}")
            return False
