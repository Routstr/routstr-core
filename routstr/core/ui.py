from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .logging import get_logger
from .settings import settings as global_settings

logger = get_logger(__name__)


def setup_ui_routes(app: FastAPI) -> None:
    ui_dist_path = Path(__file__).parent.parent.parent / "ui_out"

    if ui_dist_path.exists() and ui_dist_path.is_dir():
        logger.info(f"Serving static UI from {ui_dist_path}")

        app.mount(
            "/_next",
            StaticFiles(directory=ui_dist_path / "_next", check_dir=True),
            name="next-static",
        )

        router = APIRouter()

        @router.get("/", include_in_schema=False)
        async def serve_root_ui() -> FileResponse:
            return FileResponse(ui_dist_path / "index.html")

        @router.get("/index.txt", include_in_schema=False)
        async def redirect_index_txt() -> RedirectResponse:
            return RedirectResponse("/")

        @router.get("/admin")
        async def admin_redirect() -> FileResponse:
            return FileResponse(ui_dist_path / "index.html")

        @router.get("/dashboard", include_in_schema=False)
        async def serve_dashboard_ui() -> FileResponse:
            return FileResponse(ui_dist_path / "index.html")

        @router.get("/login", include_in_schema=False)
        async def serve_login_ui() -> FileResponse:
            return FileResponse(ui_dist_path / "login" / "index.html")

        @router.get("/login/index.txt", include_in_schema=False)
        async def redirect_login_index_txt() -> RedirectResponse:
            return RedirectResponse("/login")

        @router.get("/model", include_in_schema=False)
        async def serve_models_ui() -> FileResponse:
            return FileResponse(ui_dist_path / "model" / "index.html")

        @router.get("/model/index.txt", include_in_schema=False)
        async def redirect_model_index_txt() -> RedirectResponse:
            return RedirectResponse("/model")

        @router.get("/providers", include_in_schema=False)
        async def serve_providers_ui() -> FileResponse:
            return FileResponse(ui_dist_path / "providers" / "index.html")

        @router.get("/providers/index.txt", include_in_schema=False)
        async def redirect_providers_index_txt() -> RedirectResponse:
            return RedirectResponse("/providers")

        @router.get("/settings", include_in_schema=False)
        async def serve_settings_ui() -> FileResponse:
            return FileResponse(ui_dist_path / "settings" / "index.html")

        @router.get("/settings/index.txt", include_in_schema=False)
        async def redirect_settings_index_txt() -> RedirectResponse:
            return RedirectResponse("/settings")

        @router.get("/transactions", include_in_schema=False)
        async def serve_transactions_ui() -> FileResponse:
            return FileResponse(ui_dist_path / "transactions" / "index.html")

        @router.get("/transactions/index.txt", include_in_schema=False)
        async def redirect_transactions_index_txt() -> RedirectResponse:
            return RedirectResponse("/transactions")

        @router.get("/balances", include_in_schema=False)
        async def serve_balances_ui() -> FileResponse:
            return FileResponse(ui_dist_path / "balances" / "index.html")

        @router.get("/balances/index.txt", include_in_schema=False)
        async def redirect_balances_index_txt() -> RedirectResponse:
            return RedirectResponse("/balances")

        @router.get("/logs", include_in_schema=False)
        async def serve_logs_ui() -> FileResponse:
            return FileResponse(ui_dist_path / "logs" / "index.html")

        @router.get("/logs/index.txt", include_in_schema=False)
        async def redirect_logs_index_txt() -> RedirectResponse:
            return RedirectResponse("/logs")

        @router.get("/usage", include_in_schema=False)
        async def serve_usage_ui() -> FileResponse:
            return FileResponse(ui_dist_path / "usage" / "index.html")

        @router.get("/usage/index.txt", include_in_schema=False)
        async def redirect_usage_index_txt() -> RedirectResponse:
            return RedirectResponse("/usage")

        @router.get("/unauthorized", include_in_schema=False)
        async def serve_unauthorized_ui() -> FileResponse:
            return FileResponse(ui_dist_path / "unauthorized" / "index.html")

        @router.get("/unauthorized/index.txt", include_in_schema=False)
        async def redirect_unauthorized_index_txt() -> RedirectResponse:
            return RedirectResponse("/unauthorized")

        @router.get("/favicon.ico", include_in_schema=False)
        async def serve_favicon() -> FileResponse:
            icon_path = ui_dist_path / "icon.ico"
            if icon_path.exists():
                return FileResponse(icon_path)
            return FileResponse(ui_dist_path / "favicon.ico")

        @router.get("/icon.ico", include_in_schema=False)
        async def serve_icon() -> FileResponse:
            return FileResponse(ui_dist_path / "icon.ico")

        app.include_router(router)

        app.mount(
            "/static",
            StaticFiles(directory=ui_dist_path, check_dir=True),
            name="ui-static",
        )
    else:
        logger.warning(
            f"UI dist directory not found at {ui_dist_path}, skipping static file serving",
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
