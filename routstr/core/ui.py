from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .logging import get_logger
from .settings import settings as global_settings

logger = get_logger(__name__)


def setup_ui_routes(app: FastAPI) -> None:
    UI_DIST_PATH = Path(__file__).parent.parent.parent / "ui_out"

    if UI_DIST_PATH.exists() and UI_DIST_PATH.is_dir():
        logger.info(f"Serving static UI from {UI_DIST_PATH}")

        app.mount(
            "/_next",
            StaticFiles(directory=UI_DIST_PATH / "_next", check_dir=True),
            name="next-static",
        )

        router = APIRouter()

        @router.get("/", include_in_schema=False)
        async def serve_root_ui() -> FileResponse:
            return FileResponse(UI_DIST_PATH / "index.html")

        # Add explicit route for /index.txt to redirect to /
        @router.get("/index.txt", include_in_schema=False)
        async def redirect_index_txt() -> RedirectResponse:
            return RedirectResponse("/")

        @router.get("/admin")
        async def admin_redirect() -> FileResponse:
            return FileResponse(UI_DIST_PATH / "index.html")

        @router.get("/dashboard", include_in_schema=False)
        async def serve_dashboard_ui() -> FileResponse:
            return FileResponse(UI_DIST_PATH / "index.html")

        @router.get("/login", include_in_schema=False)
        async def serve_login_ui() -> FileResponse:
            return FileResponse(UI_DIST_PATH / "login" / "index.html")

        # Add explicit route for /login/index.txt to redirect to /login
        @router.get("/login/index.txt", include_in_schema=False)
        async def redirect_login_index_txt() -> RedirectResponse:
            return RedirectResponse("/login")

        @router.get("/model", include_in_schema=False)
        async def serve_models_ui() -> FileResponse:
            return FileResponse(UI_DIST_PATH / "model" / "index.html")

        # Add explicit route for /model/index.txt to redirect to /model
        @router.get("/model/index.txt", include_in_schema=False)
        async def redirect_model_index_txt() -> RedirectResponse:
            return RedirectResponse("/model")

        @router.get("/providers", include_in_schema=False)
        async def serve_providers_ui() -> FileResponse:
            return FileResponse(UI_DIST_PATH / "providers" / "index.html")

        # Add explicit route for /providers/index.txt to redirect to /providers
        @router.get("/providers/index.txt", include_in_schema=False)
        async def redirect_providers_index_txt() -> RedirectResponse:
            return RedirectResponse("/providers")

        @router.get("/settings", include_in_schema=False)
        async def serve_settings_ui() -> FileResponse:
            return FileResponse(UI_DIST_PATH / "settings" / "index.html")

        # Add explicit route for /settings/index.txt to redirect to /settings
        @router.get("/settings/index.txt", include_in_schema=False)
        async def redirect_settings_index_txt() -> RedirectResponse:
            return RedirectResponse("/settings")

        @router.get("/transactions", include_in_schema=False)
        async def serve_transactions_ui() -> FileResponse:
            return FileResponse(UI_DIST_PATH / "transactions" / "index.html")

        # Add explicit route for /transactions/index.txt to redirect to /transactions
        @router.get("/transactions/index.txt", include_in_schema=False)
        async def redirect_transactions_index_txt() -> RedirectResponse:
            return RedirectResponse("/transactions")

        @router.get("/balances", include_in_schema=False)
        async def serve_balances_ui() -> FileResponse:
            return FileResponse(UI_DIST_PATH / "balances" / "index.html")

        # Add explicit route for /balances/index.txt to redirect to /balances
        @router.get("/balances/index.txt", include_in_schema=False)
        async def redirect_balances_index_txt() -> RedirectResponse:
            return RedirectResponse("/balances")

        @router.get("/logs", include_in_schema=False)
        async def serve_logs_ui() -> FileResponse:
            return FileResponse(UI_DIST_PATH / "logs" / "index.html")

        # Add explicit route for /logs/index.txt to redirect to /logs
        @router.get("/logs/index.txt", include_in_schema=False)
        async def redirect_logs_index_txt() -> RedirectResponse:
            return RedirectResponse("/logs")

        @router.get("/usage", include_in_schema=False)
        async def serve_usage_ui() -> FileResponse:
            return FileResponse(UI_DIST_PATH / "usage" / "index.html")

        # Add explicit route for /usage/index.txt to redirect to /usage
        @router.get("/usage/index.txt", include_in_schema=False)
        async def redirect_usage_index_txt() -> RedirectResponse:
            return RedirectResponse("/usage")

        @router.get("/unauthorized", include_in_schema=False)
        async def serve_unauthorized_ui() -> FileResponse:
            return FileResponse(UI_DIST_PATH / "unauthorized" / "index.html")

        # Add explicit route for /unauthorized/index.txt to redirect to /unauthorized
        @router.get("/unauthorized/index.txt", include_in_schema=False)
        async def redirect_unauthorized_index_txt() -> RedirectResponse:
            return RedirectResponse("/unauthorized")

        @router.get("/favicon.ico", include_in_schema=False)
        async def serve_favicon() -> FileResponse:
            icon_path = UI_DIST_PATH / "icon.ico"
            if icon_path.exists():
                return FileResponse(icon_path)
            return FileResponse(UI_DIST_PATH / "favicon.ico")

        @router.get("/icon.ico", include_in_schema=False)
        async def serve_icon() -> FileResponse:
            return FileResponse(UI_DIST_PATH / "icon.ico")

        app.include_router(router)

        app.mount(
            "/static",
            StaticFiles(directory=UI_DIST_PATH, check_dir=True),
            name="ui-static",
        )
    else:
        logger.warning(
            f"UI dist directory not found at {UI_DIST_PATH}, skipping static file serving"
        )

        router = APIRouter()

        @router.get("/", include_in_schema=False)
        async def root_fallback() -> dict:
            return {
                "name": global_settings.name,
                "description": global_settings.description,
                "version": app.version,
                "status": "running",
                "ui": "not available",
            }

        app.include_router(router)
