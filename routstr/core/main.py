import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from ..balance import balance_router, deprecated_wallet_router
from ..discovery import providers_cache_refresher, providers_router
from ..nip91 import announce_provider
from ..payment.models import (
    models_router,
    update_sats_pricing,
)
from ..payment.price import update_prices_periodically
from ..proxy import initialize_upstreams, proxy_router, refresh_model_maps_periodically
from ..wallet import periodic_payout
from .admin import admin_router
from .db import create_session, init_db, run_migrations
from .exceptions import general_exception_handler, http_exception_handler
from .logging import get_logger, setup_logging
from .middleware import LoggingMiddleware
from .settings import SettingsService
from .settings import settings as global_settings

# Initialize logging first
setup_logging()
logger = get_logger(__name__)

if os.getenv("VERSION_SUFFIX") is not None:
    __version__ = f"0.2.0-{os.getenv('VERSION_SUFFIX')}"
else:
    __version__ = "0.2.0-dev"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Application startup initiated", extra={"version": __version__})

    btc_price_task = None
    pricing_task = None
    payout_task = None
    nip91_task = None
    providers_task = None
    models_refresh_task = None
    model_maps_refresh_task = None

    try:
        # Run database migrations on startup
        # This ensures the database schema is always up-to-date in production
        # Migrations are idempotent - running them multiple times is safe
        logger.info("Running database migrations")
        run_migrations()

        # Initialize database connection pools
        # This creates any tables that might not be tracked by migrations yet
        await init_db()

        # Initialize application settings (env -> computed -> DB precedence)
        async with create_session() as session:
            s = await SettingsService.initialize(session)

        # Apply app metadata from settings
        try:
            app.title = s.name
            app.description = s.description
        except Exception:
            pass

        # await ensure_models_bootstrapped()

        from ..payment.price import _update_prices
        from ..proxy import get_upstreams
        from ..upstream import refresh_upstreams_models_periodically

        await _update_prices()
        await initialize_upstreams()

        btc_price_task = asyncio.create_task(update_prices_periodically())
        pricing_task = asyncio.create_task(update_sats_pricing())
        if global_settings.models_refresh_interval_seconds > 0:
            models_refresh_task = asyncio.create_task(
                refresh_upstreams_models_periodically(get_upstreams())
            )
        model_maps_refresh_task = asyncio.create_task(refresh_model_maps_periodically())
        payout_task = asyncio.create_task(periodic_payout())
        nip91_task = asyncio.create_task(announce_provider())
        providers_task = asyncio.create_task(providers_cache_refresher())

        yield

    except Exception as e:
        logger.error(
            "Application startup failed",
            extra={"error": str(e), "error_type": type(e).__name__},
        )
        raise
    finally:
        logger.info("Application shutdown initiated")

        if btc_price_task is not None:
            btc_price_task.cancel()
        if pricing_task is not None:
            pricing_task.cancel()
        if payout_task is not None:
            payout_task.cancel()
        if nip91_task is not None:
            nip91_task.cancel()
        if providers_task is not None:
            providers_task.cancel()
        if models_refresh_task is not None:
            models_refresh_task.cancel()
        if model_maps_refresh_task is not None:
            model_maps_refresh_task.cancel()

        try:
            tasks_to_wait = []
            if btc_price_task is not None:
                tasks_to_wait.append(btc_price_task)
            if pricing_task is not None:
                tasks_to_wait.append(pricing_task)
            if payout_task is not None:
                tasks_to_wait.append(payout_task)
            if nip91_task is not None:
                tasks_to_wait.append(nip91_task)
            if providers_task is not None:
                tasks_to_wait.append(providers_task)
            if models_refresh_task is not None:
                tasks_to_wait.append(models_refresh_task)
            if model_maps_refresh_task is not None:
                tasks_to_wait.append(model_maps_refresh_task)

            if tasks_to_wait:
                await asyncio.gather(*tasks_to_wait, return_exceptions=True)
            logger.info("Background tasks stopped successfully")
        except Exception as e:
            logger.error(
                "Error stopping background tasks",
                extra={"error": str(e), "error_type": type(e).__name__},
            )


app = FastAPI(version=__version__, lifespan=lifespan)


app.add_middleware(
    CORSMiddleware,
    allow_origins=global_settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["x-routstr-request-id"],
)

# Add logging middleware
app.add_middleware(LoggingMiddleware)

# Add exception handlers
app.add_exception_handler(HTTPException, http_exception_handler)  # type: ignore
app.add_exception_handler(Exception, general_exception_handler)


@app.get("/v1/info")
async def info() -> dict:
    return {
        "name": global_settings.name,
        "description": global_settings.description,
        "version": __version__,
        "npub": global_settings.npub,
        "mints": global_settings.cashu_mints,
        "http_url": global_settings.http_url,
        "onion_url": global_settings.onion_url,
        "models": [],  # kept for back-compat; prefer /v1/models
    }


@app.get("/v1/providers")
async def providers() -> RedirectResponse:
    return RedirectResponse("/v1/providers/")


UI_DIST_PATH = Path(__file__).parent.parent.parent / "ui_out"

if UI_DIST_PATH.exists() and UI_DIST_PATH.is_dir():
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

    @app.get("/admin")
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
        "/static", StaticFiles(directory=UI_DIST_PATH, check_dir=True), name="ui-static"
    )
else:
    logger.warning(
        f"UI dist directory not found at {UI_DIST_PATH}, skipping static file serving"
    )

    @app.get("/", include_in_schema=False)
    async def root_fallback() -> dict:
        return {
            "name": global_settings.name,
            "description": global_settings.description,
            "version": __version__,
            "status": "running",
            "ui": "not available",
        }


app.include_router(models_router)
app.include_router(admin_router)
app.include_router(balance_router)
app.include_router(deprecated_wallet_router)
app.include_router(providers_router)
app.include_router(proxy_router)
