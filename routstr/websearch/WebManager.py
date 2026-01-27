import asyncio
import json
from typing import Any, Optional, Tuple

from ..core.logging import get_logger
from ..core.settings import settings
from .BaseWebChunk import BaseWebChunk
from .BaseWebRAG import BaseWebRAG
from .BaseWebRank import BaseWebRank
from .BaseWebScrape import BaseWebScrape
from .BaseWebSearch import BaseWebSearch
from .CustomRAG import CustomRAG
from .types import (
    RAGProvider,
    SearchResult,
    WebChunkProvider,
    WebRankProvider,
    WebScrapeProvider,
    WebSearchProvider,
)

logger = get_logger(__name__)


class WebManager:
    """
    Manager class for web search and RAG functionality.
    Handles provider initialization, caching, and request enhancement.
    """

    def __init__(self) -> None:
        self._rag_provider: Optional[BaseWebRAG] = None
        self._search_provider: Optional[BaseWebSearch] = None
        self._scraper_provider: Optional[BaseWebScrape] = None
        self._chunker_provider: Optional[BaseWebChunk] = None
        self._rank_provider: Optional[BaseWebRank] = None

    def is_rag_enabled(self) -> bool:
        """Check if RAG is enabled in settings."""
        if not settings.web_rag_provider:
            return False
        return settings.web_rag_provider.lower() != RAGProvider.DISABLED

    async def get_rag_provider(self) -> Optional[BaseWebRAG]:
        """
        Get RAG provider based on RAG_PROVIDER configuration.
        This only returns true all-in-one RAG providers (like Tavily).

        Returns:
            BaseWebRAG instance or None if not available
        """
        if self._rag_provider:
            return self._rag_provider

        if not settings.web_rag_provider:
            logger.debug("No RAG_PROVIDER configured")
            return None

        try:
            provider_enum = RAGProvider(settings.web_rag_provider.lower())
        except ValueError:
            logger.error(f"Unknown RAG provider: {settings.web_rag_provider}")
            return None

        if provider_enum == RAGProvider.DISABLED:
            logger.info("RAG is disabled")
            return None

        match provider_enum:
            case RAGProvider.TAVILY:
                try:
                    from .TavilyWebRAG import TavilyWebRAG

                    if not settings.tavily_api_key:  # TODO: I could move this to the init function
                        logger.warning(
                            "Tavily selected as RAG provider but no API key configured"
                        )
                        return None
                    tavily = TavilyWebRAG(api_key=settings.tavily_api_key)
                    if not await tavily.check_availability():
                        logger.warning(
                            "Tavily availability check failed - service may be unavailable or API key invalid"
                        )
                        return None

                    logger.info("Using Tavily as RAG provider")
                    self._rag_provider = tavily
                    return self._rag_provider
                except Exception as e:
                    logger.error(f"Failed to initialize TavilyWebRAG: {e}")
                    return None

            case RAGProvider.EXA:
                try:
                    from .ExaWebRAG import ExaWebRAG

                    if not settings.exa_api_key:
                        logger.warning(
                            "Exa selected as RAG provider but no API key configured"
                        )
                        return None
                    exa = ExaWebRAG(api_key=settings.exa_api_key)
                    if not await exa.check_availability():
                        logger.warning(
                            "Exa availability check failed - service may be unavailable or API key invalid"
                        )
                        return None
                    logger.info("Using Exa as RAG provider")
                    self._rag_provider = exa
                    return self._rag_provider
                except Exception as e:
                    logger.error(f"Failed to initialize ExaWebRAG: {e}")
                    return None

            case RAGProvider.CUSTOM:
                try:
                    # Initialize stages asynchonously
                    (
                        search,
                        scrape,
                        chunk,
                        rank,
                    ) = await asyncio.gather(
                        self.get_web_search_provider(),
                        self.get_web_scraper_provider(),
                        self.get_web_chunker_provider(),
                        self.get_web_ranker_provider(),
                    )

                    # Check for missing components
                    if not (search and scrape and chunk and rank):
                        components = {
                            "search": search,
                            "scraper": scrape,
                            "chunker": chunk,
                            "ranker": rank,
                        }
                        missing = [
                            name
                            for name, instance in components.items()
                            if instance is None
                        ]
                        logger.warning(
                            f"Custom RAG failed to initialize. Missing: {', '.join(missing)}"
                        )
                        return None

                    # 3. Success: Mypy now knows these are not None
                    logger.info("Successfully initialized Custom RAG pipeline")
                    custom_rag = CustomRAG(search, scrape, chunk, rank)

                    if not await custom_rag.check_availability():
                        logger.error(
                            "CustomRAG initialized but some components are not available"
                        )
                        return None
                    self._rag_provider = custom_rag
                    return self._rag_provider

                except Exception as e:
                    logger.error(f"Failed to initialize CustomRAG: {e}")
                    return None

    async def get_web_search_provider(self) -> Optional[BaseWebSearch]:
        """
        Get web search provider based on WEB_SEARCH_PROVIDER configuration.
        This only returns web search providers (like Serper)

        Returns:
            Web search provider instance or None if not available
        """
        if self._search_provider:
            return self._search_provider

        if not settings.web_search_provider:
            logger.debug("No WEB_SEARCH_PROVIDER configured")
            return None

        try:
            provider_enum = WebSearchProvider(settings.web_search_provider.lower())
        except ValueError:
            logger.error(f"Unknown web search provider: {settings.web_search_provider}")
            return None

        match provider_enum:
            case WebSearchProvider.SERPER:
                try:
                    from .SerperWebSearch import SerperWebSearch

                    if not settings.serper_api_key:
                        logger.warning("Serper provider selected but no API key configured")
                        return None
                    self._search_provider = SerperWebSearch(
                        api_key=settings.serper_api_key
                    )
                    return self._search_provider
                except Exception as e:
                    logger.error(f"Failed to initialize SerperWebSearch: {e}")
                    return None

    async def get_web_scraper_provider(self) -> Optional[BaseWebScrape]:
        """
        Get web scraper provider based on configuration.

        Returns:
            Web scraper provider instance or None if not available
        """
        if self._scraper_provider:
            return self._scraper_provider

        provider_name = settings.web_scraper_provider.lower()
        provider_enum = None

        if not provider_name or provider_name == "none":
            logger.info("No web scraper configured, defaulting to 'http'")
            provider_enum = WebScrapeProvider.HTTP
        else:
            try:
                provider_enum = WebScrapeProvider(provider_name)
            except ValueError:
                logger.info(
                    f"Unknown web scraper provider '{provider_name}', defaulting to 'http'"
                )
                provider_enum = WebScrapeProvider.HTTP

        match provider_enum:
            case WebScrapeProvider.HTTP:
                try:
                    from .HTTPWebScrape import HTTPWebScrape

                    self._scraper_provider = HTTPWebScrape()
                    return self._scraper_provider
                except Exception as e:
                    logger.error(f"Failed to initialize HTTPWebScrape: {e}")
                    return None

    async def get_web_chunker_provider(self) -> Optional[BaseWebChunk]:
        """
        Get chunker provider based on configuration.

        Returns:
            Chunker provider instance or None if not available
        """
        if self._chunker_provider:
            return self._chunker_provider

        chunker_name = settings.web_chunker_provider.lower()
        provider_enum = None

        if not chunker_name or chunker_name == "none":
            logger.info("No chunker configured, defaulting to 'recursive'")
            provider_enum = WebChunkProvider.RECURSIVE
        else:
            try:
                provider_enum = WebChunkProvider(chunker_name)
            except ValueError:
                logger.info(
                    f"Unknown chunker provider '{chunker_name}', defaulting to 'recursive'"
                )
                provider_enum = WebChunkProvider.RECURSIVE

        match provider_enum:
            case WebChunkProvider.FIXED:
                try:
                    from .FixedSizeChunker import FixedSizeChunker

                    self._chunker_provider = FixedSizeChunker(
                        chunk_size=settings.max_chunk_size
                    )
                    return self._chunker_provider
                except Exception as e:
                    logger.error(f"Failed to initialize FixedSizeChunker: {e}")
                    return None
            case WebChunkProvider.RECURSIVE:
                try:
                    from .RecursiveChunker import RecursiveChunker
                    
                    self._chunker_provider = RecursiveChunker(
                        chunk_size=settings.max_chunk_size
                    )
                    return self._chunker_provider
                except Exception as e:
                    logger.error(f"Failed to initialize RecursiveChunker: {e}")
                    return None

    async def get_web_ranker_provider(self) -> Optional[BaseWebRank]:
        """
        Get ranker provider based on configuration.
        Returns: Ranker provider instance or None if not available
        """
        if self._rank_provider:
            return self._rank_provider

        ranker_name = settings.web_ranking_provider.lower()
        provider_enum = None

        if not ranker_name or ranker_name == "none":
            logger.info("No ranker configured, defaulting to 'bm25'")
            provider_enum = WebRankProvider.BM25
        else:
            try:
                provider_enum = WebRankProvider(ranker_name)
            except ValueError:
                logger.info(
                    f"Unknown ranker provider '{ranker_name}', defaulting to 'bm25'"
                )
                provider_enum = WebRankProvider.BM25

        match provider_enum:
            case WebRankProvider.BM25:
                try:
                    from .BM25WebRank import BM25WebRank

                    self._rank_provider = BM25WebRank(max_chunks_per_source = settings.max_chunks_per_source)
                    return self._rank_provider
                except Exception as e:
                    logger.error(f"Failed to initialize BM25WebRank: {e}")
                    return None

    async def enhance_request_with_web_context(
        self, request_body: bytes
    ) -> dict[str, Any]:
        """
        Enhance AI request with web search context using configured RAG provider.

        This method uses the unified RAG interface that handles the complete pipeline:
        - All-in-one providers (Tavily, Exa): Search + extract + chunk in one call
        - Custom provider: Manual pipeline with separate search, scrape, and chunk components

        Args:
            request_body: The original request body as bytes

        Returns:
            Dict containing:
            - 'body': Enhanced request body as bytes
            - 'sources': List of source strings
            - 'success': Boolean indicating if RAG was successful and yielded results
        """
        try:
            # Get configured RAG provider (all-in-one or custom)
            rag_provider = await self.get_rag_provider()

            if not rag_provider:
                logger.warning("No RAG provider available, cannot enhance request")
                return {"body": request_body, "sources": {}, "success": False}

            # Extract query from request
            extracted_query = self._extract_query_from_request_body(request_body)

            if not extracted_query:
                return {"body": request_body, "sources": {}, "success": False}

            # Perform complete RAG pipeline (handles all complexity internally)
            max_web_searches = settings.web_search_max_results
            search_result = await rag_provider.retrieve_context(
                extracted_query, max_web_searches
            )

            # Log precise timing summary
            timings = search_result.time_ms
            total_time = timings.get("total", "N/A")
            details = ", ".join([f"{k}: {v}ms" for k, v in timings.items() if k != "total"])
            detail_str = f" ({details})" if details else ""
            
            logger.info(
                f"RAG [{rag_provider.provider_name}] completed in {total_time}ms{detail_str}"
            )

            # Inject context into request
            enhanced_body, sources = await self._inject_web_context_into_request(
                request_body, search_result, extracted_query
            )

            # If we have results, it's a success
            success = bool(search_result and search_result.webpages)
            return {"body": enhanced_body, "sources": sources, "success": success}

        except Exception as e:
            logger.error(
                f"Failed to enhance request with web context: {e}",
                extra={
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "rag_provider": settings.web_rag_provider,
                },
            )
            return {"body": request_body, "sources": [], "success": False}

    @staticmethod
    def extract_web_search_parameter(body: bytes | None) -> Tuple[bytes | None, bool]:
        """
        Extracts the 'enable_web_search' parameter from a JSON request body.

        This function parses the body, extracts the boolean value of the
        'enable_web_search' key, and returns the body bytes with the key removed
        to prevent it from being forwarded to the upstream provider.

        Args:
            body: The raw request body as bytes

        Returns:
            A tuple containing:
            - bytes | None: The modified body as bytes, with 'enable_web_search' removed.
                            Returns the original body if parsing fails or it's not JSON.
            - bool: The extracted value of 'enable_web_search', defaulting to False.
        """
        if not body:
            return None, False

        try:
            body_dict = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            logger.warning(
                "Failed to decode or parse request body as JSON for web search extraction.",
                extra={"error": str(e), "error_type": type(e).__name__},
            )
            return body, False

        enable_web_search = bool(body_dict.pop("enable_web_search", False))

        # Serialize the modified dictionary back to bytes
        try:
            cleaned_body = json.dumps(body_dict).encode("utf-8")
            return cleaned_body, enable_web_search
        except (TypeError, ValueError) as e:
            # Log the error and return the original body
            logger.error(
                "Failed to re-serialize request body after removing web search parameter.",
                extra={"error": str(e), "error_type": type(e).__name__},
            )
            return body, enable_web_search  # Still return the flag

    def _extract_query_from_request_body(self, request_body: bytes) -> str:
        """
        Extract search query from user messages in request body.

        Args:
            request_body: The request body as bytes

        Returns:
            Extracted query string or empty string if not found
        """
        try:
            data = json.loads(request_body)
            messages = data.get("messages", [])

            # Iterate in reverse (from end to start)
            for message in reversed(messages):
                if message.get("role") == "user":
                    content = message.get("content", "")
                    # print(f"Extracted Query: {content}")
                    return content.strip()

            return ""

        except Exception as e:
            logger.warning(f"Failed to extract query from request body: {e}")
            return ""

    async def _inject_web_context_into_request(
        self, request_body: bytes, search_result: SearchResult, query: str
    ) -> tuple[bytes, dict[str, str]]:
        """
        Inject web search context into AI request, filtering out None values and empty content.

        Args:
            request_body: The original request body as bytes
            search_result: Either SearchResult object or None, if websearch failed
            query: The search query used

        Returns:
            Tuple of (Enhanced request body as bytes, List of source strings)
        """

        if not search_result or not search_result.webpages:
            return request_body, {}

        # Prepare list of sources
        sources = {str(i): res.url for i, res in enumerate(search_result.webpages, 1)}

        web_context = self._generate_xml_context(search_result, query)

        print(web_context) #TODO: DEBUG PRINT
        # Parse and enhance the request
        try:
            request_data = json.loads(request_body.decode("utf-8"))
            messages = request_data.get("messages", [])

            web_context_message = {
                "role": "system",
                "content": web_context,
            }

            # Inject context just before the last user message
            # Find the index of the last user message
            last_user_index = -1
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    last_user_index = i
                    break

            if last_user_index != -1:
                messages.insert(last_user_index, web_context_message)
            else:
                # Fallback: append if no user message found (shouldn't happen in normal chat)
                messages.append(web_context_message)

            request_data["messages"] = messages

            enhanced_request_body = json.dumps(request_data).encode("utf-8")
            logger.info(f"Successfully injected web context for query: '{query}'")
            #print(enhanced_request_body[:500])
            return enhanced_request_body, sources

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse request body for context injection: {e}")
            return request_body, {} # Return no sources on Failure
        except Exception as e:
            logger.error(f"Unexpected error during context injection: {e}")
            return request_body, {} # Return no sources on Failure



    def _generate_xml_context(self, search_result: SearchResult, query: str) -> str:
        """Helper to build the structured XML string."""
        parts = ["<search_results>"]
        parts.append(f"Websearch yielded {len(search_result.webpages)} results for query: '{query}'")
        
        if search_result.summary:
            parts.append(f"<search_summary>{search_result.summary}</search_summary>")

        for i, page in enumerate(search_result.webpages, 1):
            res_block = [f'<source id="{i}">']
            res_block.append(f"  <title>{page.title or 'No Title'}</title>")
            res_block.append(f"  <url>{page.url}</url>")
            
            if page.publication_date:
                res_block.append(f"  <published>{page.publication_date}</published>")
            
            if page.summary:
                res_block.append(f" <summary>{page.summary}</summary>")

            if page.chunks:
                content = " [...] ".join(page.chunks)
                res_block.append(f"  <content> {content} </content>")
            else:
                res_block.append("  <error>No extractable content available for this source.</error>")
                
            res_block.append("</source>")
            parts.append("\n".join(res_block))

        # Instructions for the LLM
        instructions = [
            "Use the sources above to answer the user's request as accurately as possible.",
            "If the sources do not contain enough information to answer the query, inform the user that the provided context is insufficient instead of speculating.",
            "Cite sources using their ID in brackets (e.g. [1]), formatted as markdown superscript (^[1]^).",
            "Pay attention to the <published> property, if available, which shows the date on which the website was first published.",
        ]

        parts.append("\n<instructions>")
        for instr in instructions:
            parts.append(f"  <instruction>{instr}</instruction>")
        parts.append("</instructions>")
        
        parts.append("</search_results>")
            
        return "\n".join(parts)


# Singleton instance
web_manager = WebManager()
