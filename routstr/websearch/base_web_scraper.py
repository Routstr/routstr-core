"""
Web scraping module for extracting content from URLs.

This module provides web scraping functionality to extract clean text content
from web pages found by the search module. It includes dummy implementations
for testing and development.
"""

import asyncio
import os
import re
from abc import ABC, abstractmethod
from dataclasses import replace
from datetime import datetime
from typing import List

from ..core.logging import get_logger
from .types import SearchResult, WebPage

logger = get_logger(__name__)


class ScrapeFailureError(Exception):
    """Custom exception for controlled scraping failures."""

    pass


class BaseWebScraper(ABC):
    """
    Base class for web scrapers.

    Handles the orchestration of concurrent scraping tasks, logging, and
    result aggregation. Subclasses are responsible for the specific
    transport layer (HTTP, Browser, etc.).
    """

    scraper_name: str = "base"

    def __init__(self, output_dir: str = "scraped_html"):
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)  # debugging TODO: remove

    @abstractmethod
    async def scrape_url(self, webpage: WebPage) -> WebPage:
        """
        Scrape content for a single webpage object.

        Args:
            webpage: The partially populated WebPage (contains URL).

        Returns:
            A new WebPage object with 'content' (+ metadata fields) populated.
            If scraping fails, return the original object (with content=None).
        """
        pass

    @abstractmethod
    async def check_availability(self) -> bool:
        """
        Check if the scraping implementation is available.
        Can include imported libraries or API health checks
        Returns: True if available, False otherwise.
        """

    async def scrape_webpages(
        self, webpages: List[WebPage], max_concurrent: int = 10
    ) -> List[WebPage]:
        """
        Orchestrates concurrent scraping of multiple webpages.
        """
        semaphore = asyncio.Semaphore(max_concurrent)

        async def _bounded_scrape(page: WebPage) -> WebPage:
            async with semaphore:
                try:
                    return await self.scrape_url(page)
                except Exception as e:
                    logger.error(f"Critical error scraping {page.url}: {e}")
                    return page

        tasks = [_bounded_scrape(page) for page in webpages]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        valid_results = []
        for res in results:
            if isinstance(res, WebPage):
                valid_results.append(res)
            else:
                logger.error(f"Task failed with exception: {res}")

        return valid_results

    async def scrape_search_results(self, search_result: SearchResult) -> SearchResult:
        """
        Main Entry Point: Takes a SearchResult, scrapes URLs, returns updated SearchResult.

        Orchestrates the concurrent scraping of all webpages found in the
        SearchResult. Updates each webpage with its extracted text content.

        Args:
            search_result: The SearchResult containing URLs to scrape.

        Returns:
            A new SearchResult with 'content' populated for each webpage.
        """
        if not search_result.webpages:
            logger.warning("No results to scrape")
            return search_result

        pages_to_scrape = search_result.webpages
        count = len(pages_to_scrape)
        logger.info(f"Scraping {count} URLs using {self.scraper_name}")

        start_time = datetime.now()

        scraped_pages = await self.scrape_webpages(pages_to_scrape)

        scrape_time_ms = int((datetime.now() - start_time).total_seconds() * 1000)
        success_count = len([p for p in scraped_pages if p.content])

        logger.info(
            f"Scraped {success_count}/{count} URLs successfully in {scrape_time_ms}ms",
            extra={
                "scraping_summary": {
                    "successful": success_count,
                    "failed": count - success_count,
                    "total": count,
                    "ms": scrape_time_ms,
                }
            },
        )
        return replace(search_result, webpages=scraped_pages)

    def _sanitize_filename(self, url: str) -> str:
        """Create a safe filename from a URL."""
        # Remove protocol
        filename = url.replace("https://", "").replace("http://", "")
        # Replace path separators and other unsafe characters
        filename = re.sub(r'[\\/:*?"<>|]', "_", filename)
        # Limit length and add .html extension
        return f"{filename[:250]}.txt"

    async def _write_to_file(self, filename: str, content: str) -> None:
        """Asynchronously write raw HTML content to a file."""
        try:
            filename = self._sanitize_filename(filename)
            filepath = os.path.join(self.output_dir, filename)

            def write_file() -> None:
                with open(filepath, "w") as f:
                    f.write(filename)
                    f.write("\n")
                    f.write(content)

            await asyncio.to_thread(write_file)

        except Exception as e:
            logger.error(f"Failed to write HTML for {filename} to file: {e}")
