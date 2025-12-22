"""HTTP client utilities for upstream requests."""

from __future__ import annotations

import traceback
from typing import TYPE_CHECKING, Callable, Mapping

import httpx
from fastapi import BackgroundTasks, Request
from fastapi.responses import StreamingResponse

from ...core import get_logger

if TYPE_CHECKING:
    from ...payment.models import Model

logger = get_logger(__name__)


class HttpForwarder:
    """Handles HTTP request forwarding to upstream services."""

    def __init__(self, base_url: str):
        self.base_url = base_url

    def _prepare_path(self, path: str) -> str:
        """Prepare path by removing v1/ prefix if present."""
        if path.startswith("v1/"):
            path = path.replace("v1/", "")
        return path

    async def forward_request(
        self,
        request: Request,
        path: str,
        headers: dict,
        request_body: bytes | None,
        query_params: Mapping[str, str] | None,
        transform_body_func: Callable | None = None,
        model_obj: Model | None = None,
    ) -> httpx.Response:
        """Forward HTTP request to upstream service.

        Args:
            request: Original FastAPI request
            path: Request path
            headers: Prepared headers for upstream
            request_body: Request body bytes, if any
            query_params: Query parameters for the request
            transform_body_func: Optional function to transform request body
            model_obj: Model object for request body transformation

        Returns:
            Response from upstream service

        Raises:
            httpx.RequestError: If request fails
            Exception: For other unexpected errors
        """
        path = self._prepare_path(path)
        url = f"{self.base_url}/{path}"

        # Transform body if function provided
        transformed_body = request_body
        if transform_body_func and request_body and model_obj:
            transformed_body = transform_body_func(request_body, model_obj)

        logger.info(
            "Forwarding request to upstream",
            extra={
                "url": url,
                "method": request.method,
                "path": path,
                "has_request_body": request_body is not None,
            },
        )

        client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(retries=1),
            timeout=None,
        )

        try:
            if transformed_body is not None:
                response = await client.send(
                    client.build_request(
                        request.method,
                        url,
                        headers=headers,
                        content=transformed_body,
                        params=query_params,
                    ),
                    stream=True,
                )
            else:
                response = await client.send(
                    client.build_request(
                        request.method,
                        url,
                        headers=headers,
                        content=request.stream(),
                        params=query_params,
                    ),
                    stream=True,
                )

            logger.info(
                "Received upstream response",
                extra={
                    "status_code": response.status_code,
                    "path": path,
                    "content_type": response.headers.get("content-type", "unknown"),
                },
            )

            return response

        except httpx.RequestError as exc:
            await client.aclose()
            error_type = type(exc).__name__
            error_details = str(exc)

            logger.error(
                "HTTP request error to upstream",
                extra={
                    "error_type": error_type,
                    "error_details": error_details,
                    "method": request.method,
                    "url": url,
                    "path": path,
                    "query_params": dict(request.query_params),
                },
            )

            if isinstance(exc, httpx.ConnectError):
                error_message = "Unable to connect to upstream service"
            elif isinstance(exc, httpx.TimeoutException):
                error_message = "Upstream service request timed out"
            elif isinstance(exc, httpx.NetworkError):
                error_message = "Network error while connecting to upstream service"
            else:
                error_message = f"Error connecting to upstream service: {error_type}"

            raise httpx.RequestError(error_message) from exc

        except Exception as exc:
            await client.aclose()
            tb = traceback.format_exc()

            logger.error(
                "Unexpected error in upstream forwarding",
                extra={
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "method": request.method,
                    "url": url,
                    "path": path,
                    "query_params": dict(request.query_params),
                    "traceback": tb,
                },
            )

            raise


class StreamingResponseWrapper:
    """Wrapper for creating streaming responses with proper cleanup."""

    @staticmethod
    def create_streaming_response(
        response: httpx.Response,
        client: httpx.AsyncClient,
    ) -> StreamingResponse:
        """Create a streaming response with background cleanup tasks.

        Args:
            response: httpx response to stream
            client: httpx client to clean up

        Returns:
            StreamingResponse with background cleanup
        """
        background_tasks = BackgroundTasks()
        background_tasks.add_task(response.aclose)
        background_tasks.add_task(client.aclose)

        logger.debug(
            "Creating streaming response",
            extra={
                "status_code": response.status_code,
                "content_type": response.headers.get("content-type", "unknown"),
            },
        )

        return StreamingResponse(
            response.aiter_bytes(),
            status_code=response.status_code,
            headers=dict(response.headers),
            background=background_tasks,
        )
