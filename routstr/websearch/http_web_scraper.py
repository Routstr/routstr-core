import logging
from dataclasses import replace
from typing import Any

import httpx

from ..core.logging import get_logger
from .base_web_scraper import BaseWebScraper, ScrapeFailureError
from .types import WebPage

logger = get_logger(__name__)

TRAFILATURA_AVAILABLE = False
trafilatura: Any = None
try:
    import trafilatura as tf
    trafilatura = tf

    logging.getLogger("trafilatura").setLevel(logging.WARNING)
    logging.getLogger("htmldate").setLevel(logging.WARNING)
    logging.getLogger("tzlocal").setLevel(logging.ERROR)
    TRAFILATURA_AVAILABLE = True
except ImportError:
    logger.warning("Trafilatura not installed. Content extraction will be unavailable.")


class HTTPWebScraper(BaseWebScraper):
    """
    High-performance scraper using HTTPX and Trafilatura.
    """

    scraper_name = "http"

    def __init__(self) -> None:
        super().__init__()
        self.client_timeout = httpx.Timeout(3.0, connect=1.5)
        self.client_headers = {
            # We could also use a Routstr specific header, but this would most likely reduce successful scrapes
            # Example: f"Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; Routstr-User/1.0; +{settings.http_url}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
            "Accept": "text/html, text/plain, application/xhtml+xml",
        }
        self.max_response_size = 5_000_000  # 5MB
        self._shared_client: httpx.AsyncClient | None = None

    async def check_availability(self) -> bool:
        """Returns True if Trafilatura is installed and available."""
        return TRAFILATURA_AVAILABLE

    async def scrape_webpages(
        self, webpages: list[WebPage], max_concurrent: int = 10
    ) -> list[WebPage]:
        """
        Creates shared HTTP client, then calls the base class to handle the looping.

        Args:
            webpages: List of webpages to scrape.
            max_concurrent: Maximum number of concurrent requests.

        Returns:
            List of scraped webpages.
        """
        async with httpx.AsyncClient(
            timeout=self.client_timeout,
            headers=self.client_headers,
            follow_redirects=True,
            verify=True,  # strict SSL
        ) as client:
            self._shared_client = client
            try:
                return await super().scrape_webpages(webpages, max_concurrent)
            finally:
                self._shared_client = None

    async def scrape_url(self, webpage: WebPage) -> WebPage:
        """
        Concrete implementation of the scraping logic.

        Args:
            webpage: The webpage object containing the URL.

        Returns:
            Updated webpage object with content.
        """
        raw_html = await self._fetch_html(webpage.url)

        if not raw_html:
            return webpage  # Return empty (content=None)

        return self._extract_data(webpage, raw_html)

    async def _fetch_html(self, url: str) -> str | None:
        """
        Handles the low-level HTTP streaming and safety checks.

        Args:
            url: The URL to fetch.

        Returns:
            The raw HTML content or None if fetching failed.
        """
        client = self._shared_client

        # Fallback: If scrape_url is called individually (not via scrape_webpages)
        local_client = None
        if not client:
            local_client = httpx.AsyncClient(
                headers=self.client_headers,
                timeout=self.client_timeout,
                follow_redirects=True,
            )
            client = local_client

        try:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise ScrapeFailureError(f"HTTP {response.status_code}")

                # MIME Check
                ctype = response.headers.get("content-type", "").lower()
                if not any(t in ctype for t in ["text/html", "text/plain", "xml"]):
                    raise ScrapeFailureError(f"Rejected non-text content: {ctype}")

                # Check Size Limit
                chunks = []
                curr_size = 0
                async for chunk in response.aiter_bytes():
                    curr_size += len(chunk)
                    if curr_size > self.max_response_size:
                        raise ScrapeFailureError("Exceeded size limit")
                    chunks.append(chunk)

                content_bytes = b"".join(chunks)
                return content_bytes.decode(
                    response.encoding or "utf-8", errors="replace"
                )

        except Exception as e:
            logger.debug(f"Fetch failed for {url}: {e}", extra={"url": url, "error": str(e)})
            return None
        finally:
            if local_client:
                await local_client.aclose()

    def _extract_data(self, webpage: WebPage, html: str) -> WebPage:
        """
        Uses Trafilatura to populate the WebPage object.

        Args:
            webpage: The webpage object.
            html: The raw HTML content.

        Returns:
            Updated webpage object with extracted metadata and content.
        """

        if not TRAFILATURA_AVAILABLE:
            return replace(webpage, content=html)

        try:
            meta_data = trafilatura.extract_metadata(
                html, default_url=webpage.url, extensive=True
            )

            content = trafilatura.extract(
                html,
                favor_precision=True,
                include_tables=True,
                deduplicate=True,
                output_format="txt",
                with_metadata=False,
            )

            return replace(
                webpage,
                content=content if content else None,
                title=meta_data.title
                if (meta_data and meta_data.title)
                else webpage.title,
                publication_date=meta_data.date
                if (meta_data and meta_data.date)
                else webpage.publication_date,
                summary=meta_data.description
                if (meta_data and meta_data.description)
                else webpage.summary,
            )

        except Exception as e:
            logger.error(
                f"Extraction failed for {webpage.url}: {e}",
                extra={"url": webpage.url, "error": str(e)},
            )
            return webpage
