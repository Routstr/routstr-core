import asyncio
import os
import json
from fastapi import APIRouter, Request, BackgroundTasks, Depends
from fastapi.responses import Response, StreamingResponse
import httpx
import re

from router.cashu import pay_out_with_new_session

from .auth import validate_bearer_key, pay_for_request, adjust_payment_for_tokens
from .db import AsyncSession, get_session

UPSTREAM_BASE_URL = os.environ["UPSTREAM_BASE_URL"]
UPSTREAM_API_KEY = os.environ.get("UPSTREAM_API_KEY", "")

proxy_router = APIRouter()


@proxy_router.api_route(
    "/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH"]
)
async def proxy(
    request: Request, path: str, session: AsyncSession = Depends(get_session)
):
    auth = request.headers.get("Authorization", "")
    bearer_key = auth.replace("Bearer ", "") if auth.startswith("Bearer ") else ""

    key = await validate_bearer_key(bearer_key, session)
    
    # Pre-validate JSON for requests that require it
    request_body = None
    if request.method in ["POST", "PUT", "PATCH"] and path.endswith("chat/completions"):
        try:
            request_body = await request.body()
            # Try to parse JSON to validate it
            if request_body:
                json.loads(request_body)
        except json.JSONDecodeError as e:
            return Response(
                content=json.dumps({
                    "error": {
                        "message": f"Invalid JSON in request body: {str(e)}",
                        "type": "invalid_request_error",
                        "code": "invalid_json"
                    }
                }),
                status_code=400,
                media_type="application/json"
            )
        except Exception as e:
            return Response(
                content=json.dumps({
                    "error": {
                        "message": "Error reading request body",
                        "type": "invalid_request_error",
                        "code": "request_error"
                    }
                }),
                status_code=400,
                media_type="application/json"
            )
    
    await pay_for_request(key, session, request, request_body)

    # Prepare headers, removing sensitive/problematic ones
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)

    if UPSTREAM_API_KEY:
        headers["Authorization"] = f"Bearer {UPSTREAM_API_KEY}"
        headers.pop("authorization", None)
    else:
        headers.pop("Authorization", None)
        headers.pop("authorization", None)

    if path.startswith("v1/"):
        path = path.replace("v1/", "")

    url = f"{UPSTREAM_BASE_URL}/{path}"
    client = httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(retries=1),
        timeout=None  # No timeout - requests can take as long as needed
    )

    try:
        # Use the pre-read body if available, otherwise stream
        if request_body is not None:
            response = await client.send(
                client.build_request(
                    request.method,
                    url,
                    headers=headers,
                    content=request_body,
                    params=request.query_params,
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
                    params=request.query_params,
                ),
                stream=True,
            )

        # For chat completions, we need to handle token-based pricing
        if path.endswith("chat/completions"):
            # Handle both streaming and non-streaming responses
            content_type = response.headers.get("content-type", "")
            is_streaming = "text/event-stream" in content_type

            if is_streaming and response.status_code == 200:
                # Process streaming response and extract cost from the last chunk
                async def stream_with_cost():
                    # Store all chunks to analyze
                    stored_chunks = []
                    usage_data_found = False

                    async for chunk in response.aiter_bytes():
                        # Store chunk for later analysis
                        stored_chunks.append(chunk)

                        # Pass through each chunk to client
                        yield chunk

                    # Process stored chunks to find usage data
                    # Start from the end and work backwards
                    for i in range(len(stored_chunks) - 1, -1, -1):
                        chunk = stored_chunks[i]
                        if not chunk or chunk == b"":
                            continue

                        try:
                            # Split by "data: " to get individual SSE events
                            events = re.split(b"data: ", chunk)
                            for event_data in events:
                                if (
                                    not event_data
                                    or event_data.strip() == b"[DONE]"
                                    or event_data.strip() == b""
                                ):
                                    continue

                                try:
                                    data = json.loads(event_data)
                                    if (
                                        "usage" in data
                                        and data["usage"] is not None
                                        and isinstance(data["usage"], dict)
                                    ):
                                        # Found usage data, calculate cost
                                        cost_data = await adjust_payment_for_tokens(
                                            key, data, session
                                        )
                                        # Format as SSE and yield
                                        cost_json = json.dumps({"cost": cost_data})
                                        yield f"data: {cost_json}\n\n".encode()
                                        usage_data_found = True
                                        break
                                except json.JSONDecodeError:
                                    continue
                            
                        except Exception as e:
                            print(f"Error processing streaming response for cost: {e}")

                background_tasks = BackgroundTasks()
                background_tasks.add_task(response.aclose)
                background_tasks.add_task(client.aclose)

                return StreamingResponse(
                    stream_with_cost(),
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    background=background_tasks,
                )

            elif response.status_code == 200 and "application/json" in content_type:
                # Handle non-streaming response
                try:
                    content = await response.aread()
                    response_json = json.loads(content)
                    cost_data = await adjust_payment_for_tokens(
                        key, response_json, session
                    )
                    response_json["cost"] = cost_data
                    return Response(
                        content=json.dumps(response_json).encode(),
                        status_code=response.status_code,
                        headers=dict(response.headers),
                        media_type="application/json",
                    )
                except json.JSONDecodeError as e:
                    print(f"Failed to parse JSON from upstream response: {e}")
                except Exception as e:
                    print(f"Error adjusting payment for tokens: {e}")
                finally:
                    await response.aclose()
                    await client.aclose()

        # For all other responses, stream the response
        background_tasks = BackgroundTasks()
        background_tasks.add_task(response.aclose)
        background_tasks.add_task(client.aclose)
        background_tasks.add_task(pay_out_with_new_session)

        return StreamingResponse(
            response.aiter_bytes(),
            status_code=response.status_code,
            headers=dict(response.headers),
            background=background_tasks,
        )

    except httpx.RequestError as exc:
        await client.aclose()
        error_type = type(exc).__name__
        error_details = str(exc)
        print(
            f"Error forwarding request to upstream: {error_type}: {error_details}\n"
            f"Request details: method={request.method}, url={url}, headers={headers}, "
            f"path={path}, query_params={dict(request.query_params)}"
        )
        
        # Provide more specific error messages based on the error type
        if isinstance(exc, httpx.ConnectError):
            error_message = "Unable to connect to upstream service"
        elif isinstance(exc, httpx.TimeoutException):
            error_message = "Upstream service request timed out"
        elif isinstance(exc, httpx.NetworkError):
            error_message = "Network error while connecting to upstream service"
        else:
            error_message = f"Error connecting to upstream service: {error_type}"
            
        return Response(
            content=json.dumps({
                "error": {
                    "message": error_message,
                    "type": "upstream_error",
                    "code": 502
                }
            }),
            status_code=502,
            media_type="application/json"
        )
    except Exception as exc:
        await client.aclose()
        import traceback
        tb = traceback.format_exc()
        print(
            f"Unexpected error: {exc}\n"
            f"Request details: method={request.method}, url={url}, headers={headers}, "
            f"path={path}, query_params={dict(request.query_params)}\n"
            f"Traceback:\n{tb}"
        )
        return Response(
            content=json.dumps({
                "error": {
                    "message": "An unexpected server error occurred",
                    "type": "internal_error",
                    "code": 500
                }
            }),
            status_code=500,
            media_type="application/json"
        )
