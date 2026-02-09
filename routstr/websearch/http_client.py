from typing import Any, Dict, Optional

import httpx

from ..core.logging import get_logger

logger = get_logger(__name__)


class HTTPClient:
    """
    A HTTP utility with centralized logging and connection pooling.
    """

    def __init__(
        self,
        base_url: str = "",
        default_headers: Optional[Dict[str, str]] = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = httpx.Timeout(timeout, connect=3.0)

        # Standard headers
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/json",
        }
        if default_headers:
            self.headers.update(default_headers)

        # Connection Pooling
        self._client = httpx.AsyncClient(
            base_url=self.base_url,  # httpx handles the joining natively!
            timeout=self.timeout,
            headers=self.headers,
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def get(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        return_json: bool = True,
    ) -> Any:
        return await self._request(
            "GET", url, params=params, headers=headers, return_json=return_json
        )

    async def post(
        self,
        url: str,
        json_data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        return_json: bool = True,
    ) -> Any:
        return await self._request(
            "POST", url, json_data=json_data, headers=headers, return_json=return_json
        )

    async def _request(
        self,
        method: str,
        url: str,
        json_data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        return_json: bool = True,
    ) -> Any:
        """
        Execute an asynchronous HTTP request with centralized logging and error handling.

        Args:
            method: HTTP method (GET, POST, etc.).
            url: Target URL.
            json_data: Optional JSON payload for POST requests.
            params: Optional query parameters.
            headers: Optional request-specific headers.
            return_json: Whether to parse the response as JSON.

        Returns:
            Parsed JSON dictionary or raw text response.

        Raises:
            Exception: For HTTP errors or network failures.
        """
        try:
            # httpx.AsyncClient(base_url=...) handles the join automatically
            response = await self._client.request(
                method=method, url=url, json=json_data, params=params, headers=headers
            )

            response.raise_for_status()

            logger.debug(
                f"HTTP {method} {url} success",
                extra={
                    "method": method,
                    "url": str(response.url),
                    "status_code": response.status_code,
                    "elapsed_ms": int(response.elapsed.total_seconds() * 1000),
                },
            )

            return response.json() if return_json else response.text

        except httpx.HTTPStatusError as e:
            logger.error(
                f"HTTP {method} {e.response.status_code}",
                extra={"url": str(e.request.url), "response": e.response.text[:200]},
            )
            raise Exception(f"Provider Error: {e.response.status_code}") from e

        except httpx.RequestError as e:
            logger.error(f"Network Error: {method} {url}", extra={"error": str(e)})
            raise Exception(f"Network Failure: {str(e)}") from e
