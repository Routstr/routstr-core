from unittest.mock import AsyncMock, patch

import pytest

from routstr.websearch.http_web_scraper import HTTPWebScraper
from routstr.websearch.types import WebPage


@pytest.mark.asyncio
async def test_http_scrape_real_extraction_logic() -> None:
    """
    Mocks the network but tests Trafilatura library.
    """
    scraper = HTTPWebScraper()

    real_html_sample = """
    <html>
        <head>
            <meta name="description" content="A summary of the crypto market today.">
        </head>
        <body>
            <article>
                <h1>Bitcoin reaches $100k</h1>
                <p>This is the actual content that should be extracted by Trafilatura.</p>
                <h2>Routstr</h2>
                <p>Routstr gains 1M users</p>
                <footer>Published on 2026-01-17</footer>
            </article>
        </body>
    </html>
    """

    # Mock network call
    with patch.object(
        HTTPWebScraper, "_fetch_html", new_callable=AsyncMock
    ) as mock_fetch:
        mock_fetch.return_value = real_html_sample

        page = WebPage(url="https://crypto-news.com/btc")

        #  This calls trafilatura.extract()
        result = await scraper.scrape_url(page)

        assert result.title == "Bitcoin reaches $100k"
        assert result.content is not None
        assert (
            "This is the actual content that should be extracted by Trafilatura."
            in result.content
        )
        assert "Routstr gains 1M users" in result.content
        assert result.summary == "A summary of the crypto market today."
        # Verify that Trafilatura cleaned the HTML tags out
        assert "<p>" not in result.content


@pytest.mark.asyncio
async def test_http_scrape_fetch_failure() -> None:
    """Tests that a failed fetch returns the original webpage gracefully."""
    scraper = HTTPWebScraper()
    page = WebPage(url="https://fail.com", title="Original Title")

    with patch.object(
        HTTPWebScraper, "_fetch_html", new_callable=AsyncMock
    ) as mock_fetch:
        mock_fetch.return_value = None  # Simulate a fetch failure (e.g., 404)

        result = await scraper.scrape_url(page)

        # Should return the original page without modifications
        assert result.url == "https://fail.com"
        assert result.title == "Original Title"
        assert result.content is None


@pytest.mark.asyncio
async def test_http_scrape_extraction_crash() -> None:
    """Tests that if trafilatura crashes, the scraper still returns the raw HTML."""
    scraper = HTTPWebScraper()
    page = WebPage(url="https://crash.com")
    html = "<html><body>Raw HTML</body></html>"

    with patch("routstr.websearch.http_web_scraper.trafilatura") as mock_traf:
        # Simulate trafilatura blowing up
        mock_traf.extract.side_effect = Exception("Crashed")

        with patch.object(
            HTTPWebScraper, "_fetch_html", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = html

            # This triggers the 'except Exception as e' in _extract_data
            result = await scraper.scrape_url(page)

            # Your code returns 'webpage' (unmodified) on extraction failure
            assert result.url == "https://crash.com"
            assert result.content is None
