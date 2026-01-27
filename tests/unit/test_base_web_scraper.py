import pytest
import asyncio
from routstr.websearch.base_web_scraper import BaseWebScraper
from routstr.websearch.types import WebPage

class MockScraper(BaseWebScraper):
    """Minimal implementation to test the Base class orchestration."""
    async def scrape_url(self, webpage: WebPage) -> WebPage:
        # Simulate a small delay
        await asyncio.sleep(0.01)
        if "fail" in webpage.url:
            raise Exception("Network Error")
        return WebPage(url=webpage.url, content="Scraped Content")

    async def check_availability(self) -> bool:
        return True

@pytest.mark.asyncio
async def test_base_scrape_error_handling() -> None:
    """
    Targets: BaseWebScrape.scrape_webpages and _bounded_scrape
    Verifies that errors don't crash the loop.
    """
    scraper = MockScraper()
    pages = [
        WebPage(url="https://ok1.com"),
        WebPage(url="https://fail.com"), # Should be caught by the try/except
        WebPage(url="https://ok2.com")
    ]
    
    # Run concurrent scrape
    results = await scraper.scrape_webpages(pages, max_concurrent=2)
    
    # Assertions
    assert len(results) == 3
    successes = [p for p in results if p.content == "Scraped Content"]
    assert len(successes) == 2
    # Ensure the failed url is still in the list 
    assert any(p.url == "https://fail.com" and p.content is None for p in results)