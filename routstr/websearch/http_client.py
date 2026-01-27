import httpx
from typing import Any, Dict, Optional
from ..core.logging import get_logger

logger = get_logger(__name__)

class HTTPClient:
    """
    A strict, high-performance HTTP utility.
    No URL guessing. Centralized logging and connection pooling.
    """
    def __init__(
        self, 
        base_url: str = "", 
        default_headers: Optional[Dict[str, str]] = None,
        timeout: float = 10.0
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
            base_url=self.base_url, # httpx handles the joining natively!
            timeout=self.timeout,
            headers=self.headers,
            follow_redirects=True
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def get(
        self, 
        url: str, 
        params: Optional[Dict[str, Any]] = None, 
        headers: Optional[Dict[str, str]] = None,
        return_json: bool = True
    ) -> Any:
        return await self._request("GET", url, params=params, headers=headers, return_json=return_json)

    async def post(
        self, 
        url: str, 
        json_data: Optional[Dict[str, Any]] = None, 
        headers: Optional[Dict[str, str]] = None,
        return_json: bool = True
    ) -> Any:
        return await self._request("POST", url, json_data=json_data, headers=headers, return_json=return_json)

    async def _request(
        self, 
        method: str, 
        url: str, 
        json_data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        return_json: bool = True
    ) -> Any:
        try:
            # httpx.AsyncClient(base_url=...) handles the join automatically
            response = await self._client.request(
                method=method,
                url=url, 
                json=json_data,
                params=params,
                headers=headers
            )
            
            response.raise_for_status()
            return response.json() if return_json else response.text

        except httpx.HTTPStatusError as e:
            logger.error(
                f"HTTP {method} {e.response.status_code}",
                extra={"url": str(e.request.url), "response": e.response.text[:200]}
            )
            raise Exception(f"Provider Error: {e.response.status_code}") from e
            
        except httpx.RequestError as e:
            logger.error(f"Network Error: {method} {url}", extra={"error": str(e)})
            raise Exception(f"Network Failure: {str(e)}") from e