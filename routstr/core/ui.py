from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .logging import get_logger
from .settings import settings as global_settings

logger = get_logger(__name__)


def setup_ui(app: FastAPI, version: str) -> None:
    UI_DIST_PATH = Path(__file__).parent.parent.parent / "ui_out"

    # Check if we have a valid local UI build
    # We require at least the index.html to be present because the directory might exist
    # but be empty (e.g. Docker volume mount before build completes)
    has_local_ui = (
        UI_DIST_PATH.exists()
        and UI_DIST_PATH.is_dir()
        and (UI_DIST_PATH / "index.html").exists()
    )

    if has_local_ui:
        logger.info(f"Serving static UI from {UI_DIST_PATH}")

        app.mount(
            "/_next",
            StaticFiles(directory=UI_DIST_PATH / "_next", check_dir=True),
            name="next-static",
        )

        @app.get("/", include_in_schema=False)
        async def serve_root_ui() -> FileResponse:
            return FileResponse(UI_DIST_PATH / "index.html")

        # Add explicit route for /index.txt to redirect to /
        @app.get("/index.txt", include_in_schema=False)
        async def redirect_index_txt() -> RedirectResponse:
            return RedirectResponse("/")

        @app.get("/admin", include_in_schema=False)
        async def admin_redirect() -> FileResponse:
            return FileResponse(UI_DIST_PATH / "index.html")

        @app.get("/dashboard", include_in_schema=False)
        async def serve_dashboard_ui() -> FileResponse:
            return FileResponse(UI_DIST_PATH / "index.html")

        @app.get("/login", include_in_schema=False)
        async def serve_login_ui() -> FileResponse:
            return FileResponse(UI_DIST_PATH / "login" / "index.html")

        # Add explicit route for /login/index.txt to redirect to /login
        @app.get("/login/index.txt", include_in_schema=False)
        async def redirect_login_index_txt() -> RedirectResponse:
            return RedirectResponse("/login")

        @app.get("/model", include_in_schema=False)
        async def serve_models_ui() -> FileResponse:
            return FileResponse(UI_DIST_PATH / "model" / "index.html")

        # Add explicit route for /model/index.txt to redirect to /model
        @app.get("/model/index.txt", include_in_schema=False)
        async def redirect_model_index_txt() -> RedirectResponse:
            return RedirectResponse("/model")

        @app.get("/providers", include_in_schema=False)
        async def serve_providers_ui() -> FileResponse:
            return FileResponse(UI_DIST_PATH / "providers" / "index.html")

        # Add explicit route for /providers/index.txt to redirect to /providers
        @app.get("/providers/index.txt", include_in_schema=False)
        async def redirect_providers_index_txt() -> RedirectResponse:
            return RedirectResponse("/providers")

        @app.get("/settings", include_in_schema=False)
        async def serve_settings_ui() -> FileResponse:
            return FileResponse(UI_DIST_PATH / "settings" / "index.html")

        # Add explicit route for /settings/index.txt to redirect to /settings
        @app.get("/settings/index.txt", include_in_schema=False)
        async def redirect_settings_index_txt() -> RedirectResponse:
            return RedirectResponse("/settings")

        @app.get("/transactions", include_in_schema=False)
        async def serve_transactions_ui() -> FileResponse:
            return FileResponse(UI_DIST_PATH / "transactions" / "index.html")

        # Add explicit route for /transactions/index.txt to redirect to /transactions
        @app.get("/transactions/index.txt", include_in_schema=False)
        async def redirect_transactions_index_txt() -> RedirectResponse:
            return RedirectResponse("/transactions")

        @app.get("/balances", include_in_schema=False)
        async def serve_balances_ui() -> FileResponse:
            return FileResponse(UI_DIST_PATH / "balances" / "index.html")

        # Add explicit route for /balances/index.txt to redirect to /balances
        @app.get("/balances/index.txt", include_in_schema=False)
        async def redirect_balances_index_txt() -> RedirectResponse:
            return RedirectResponse("/balances")

        @app.get("/logs", include_in_schema=False)
        async def serve_logs_ui() -> FileResponse:
            return FileResponse(UI_DIST_PATH / "logs" / "index.html")

        # Add explicit route for /logs/index.txt to redirect to /logs
        @app.get("/logs/index.txt", include_in_schema=False)
        async def redirect_logs_index_txt() -> RedirectResponse:
            return RedirectResponse("/logs")

        @app.get("/usage", include_in_schema=False)
        async def serve_usage_ui() -> FileResponse:
            return FileResponse(UI_DIST_PATH / "usage" / "index.html")

        # Add explicit route for /usage/index.txt to redirect to /usage
        @app.get("/usage/index.txt", include_in_schema=False)
        async def redirect_usage_index_txt() -> RedirectResponse:
            return RedirectResponse("/usage")

        @app.get("/unauthorized", include_in_schema=False)
        async def serve_unauthorized_ui() -> FileResponse:
            return FileResponse(UI_DIST_PATH / "unauthorized" / "index.html")

        # Add explicit route for /unauthorized/index.txt to redirect to /unauthorized
        @app.get("/unauthorized/index.txt", include_in_schema=False)
        async def redirect_unauthorized_index_txt() -> RedirectResponse:
            return RedirectResponse("/unauthorized")

        @app.get("/favicon.ico", include_in_schema=False)
        async def serve_favicon() -> FileResponse:
            icon_path = UI_DIST_PATH / "icon.ico"
            if icon_path.exists():
                return FileResponse(icon_path)
            return FileResponse(UI_DIST_PATH / "favicon.ico")

        @app.get("/icon.ico", include_in_schema=False)
        async def serve_icon() -> FileResponse:
            return FileResponse(UI_DIST_PATH / "icon.ico")

        app.mount(
            "/static",
            StaticFiles(directory=UI_DIST_PATH, check_dir=True),
            name="ui-static",
        )
    else:
        logger.info(
            f"Local UI not found at {UI_DIST_PATH} (checked for index.html), falling back to cloud/proxy"
        )
        if global_settings.fallback_ui_url:
            logger.info(f"Proxying UI to {global_settings.fallback_ui_url}")

            async def _proxy_ui_request(request: Request, path: str = "") -> Any:
                # Clean up the target URL
                base_url = global_settings.fallback_ui_url.rstrip("/")
                target_url = f"{base_url}/{path.lstrip('/')}"

                headers = dict(request.headers)
                headers.pop("host", None)
                headers.pop("content-length", None)

                client = httpx.AsyncClient()
                try:
                    rp_req = client.build_request("GET", target_url, headers=headers)
                    rp_resp = await client.send(rp_req, stream=True)

                    async def stream_response() -> Any:
                        try:
                            async for chunk in rp_resp.aiter_raw():
                                yield chunk
                        finally:
                            await rp_resp.aclose()
                            await client.aclose()

                    response_headers = dict(rp_resp.headers)
                    # Filter out hop-by-hop headers and others that shouldn't be proxied directly
                    for key in [
                        "transfer-encoding",
                        "connection",
                        "keep-alive",
                        "host",
                    ]:
                        response_headers.pop(key, None)

                    return StreamingResponse(
                        stream_response(),
                        status_code=rp_resp.status_code,
                        headers=response_headers,
                    )
                except Exception as e:
                    await client.aclose()
                    logger.error(f"Failed to proxy UI request to {target_url}: {e}")
                    return {"error": "UI Proxy Error", "details": str(e)}

            @app.get("/", include_in_schema=False)
            async def proxy_root_ui(request: Request) -> Any:
                return await _proxy_ui_request(request, "")

            @app.get("/_next/{path:path}", include_in_schema=False)
            async def proxy_next_assets(request: Request, path: str) -> Any:
                return await _proxy_ui_request(request, f"_next/{path}")

            @app.get("/static/{path:path}", include_in_schema=False)
            async def proxy_static_assets(request: Request, path: str) -> Any:
                return await _proxy_ui_request(request, f"static/{path}")

            # Proxy common assets that might be at root
            @app.get("/favicon.ico", include_in_schema=False)
            async def proxy_favicon(request: Request) -> Any:
                return await _proxy_ui_request(request, "favicon.ico")

            @app.get("/icon.ico", include_in_schema=False)
            async def proxy_icon(request: Request) -> Any:
                return await _proxy_ui_request(request, "icon.ico")

            # SPA routes
            for route in [
                "/admin",
                "/dashboard",
                "/login",
                "/model",
                "/providers",
                "/settings",
                "/transactions",
                "/balances",
                "/logs",
                "/usage",
                "/unauthorized",
            ]:

                @app.get(route, include_in_schema=False)
                async def proxy_spa_route(request: Request) -> Any:
                    return await _proxy_ui_request(request, "")

        else:
            logger.warning(
                f"UI dist directory not found at {UI_DIST_PATH} and no fallback_ui_url configured"
            )

            @app.get("/", include_in_schema=False)
            async def root_fallback() -> dict[str, Any]:
                return {
                    "name": global_settings.name,
                    "description": global_settings.description,
                    "version": version,
                    "status": "running",
                    "ui": "not available",
                }
