import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.responses import Response as StarletteResponse
from starlette.types import Scope

from ..auth import periodic_key_reset
from ..balance import balance_router, deprecated_wallet_router
from ..nostr import (
    announce_provider,
    providers_cache_refresher,
    publish_usage_analytics,
)
from ..nostr.discovery import providers_router
from ..payment.models import models_router, update_sats_pricing
from ..payment.price import update_prices_periodically
from ..proxy import initialize_upstreams, proxy_router, refresh_model_maps_periodically
from ..upstream.auto_topup import periodic_auto_topup
from ..wallet import periodic_payout, periodic_refund_sweep, periodic_routstr_fee_payout
from .admin import admin_router
from .db import create_session, init_db, run_migrations
from .exceptions import general_exception_handler, http_exception_handler
from .logging import get_logger, setup_logging
from .middleware import LoggingMiddleware
from .settings import SettingsService
from .settings import settings as global_settings
from .version import __version__

# Initialize logging first
setup_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Application startup initiated", extra={"version": __version__})

    btc_price_task = None
    pricing_task = None
    payout_task = None
    nip91_task = None
    analytics_task = None
    providers_task = None
    models_refresh_task = None
    model_maps_refresh_task = None
    key_reset_task = None
    auto_topup_task = None
    refund_sweep_task = None
    routstr_fee_task = None

    try:
        # Run database migrations on startup
        run_migrations()

        # Initialize database connection pools
        # This creates any tables that might not be tracked by migrations yet
        await init_db()

        # Initialize application settings (env -> computed -> DB precedence)
        async with create_session() as session:
            s = await SettingsService.initialize(session)
            if s.reset_reserved_balance_on_startup:
                from .db import reset_all_reserved_balances

                await reset_all_reserved_balances(session)

        if not s.admin_password:
            logger.warning(
                f"Admin password is not set. Visit {s.http_url or 'http://localhost:8000'}/admin to set the password."
            )

        # Apply app metadata from settings
        try:
            app.title = s.name
            app.description = s.description
        except Exception:
            pass

        # await ensure_models_bootstrapped()

        from ..payment.price import _update_prices
        from ..proxy import get_upstreams
        from ..upstream.helpers import refresh_upstreams_models_periodically

        _update_prices_task = asyncio.create_task(_update_prices())
        _initialize_upstreams_task = asyncio.create_task(initialize_upstreams())

        # ensure both setup tasks complete
        await asyncio.gather(
            _update_prices_task, _initialize_upstreams_task, return_exceptions=True
        )

        btc_price_task = asyncio.create_task(update_prices_periodically())
        pricing_task = asyncio.create_task(update_sats_pricing())
        if global_settings.models_refresh_interval_seconds > 0:
            # Pass the accessor (not its current value) so the loop sees providers
            # added/changed via reinitialize_upstreams() instead of staying pinned
            # to the startup snapshot.
            models_refresh_task = asyncio.create_task(
                refresh_upstreams_models_periodically(get_upstreams)
            )
        model_maps_refresh_task = asyncio.create_task(refresh_model_maps_periodically())
        payout_task = asyncio.create_task(periodic_payout())
        if global_settings.nsec:
            nip91_task = asyncio.create_task(announce_provider())
        analytics_task = asyncio.create_task(publish_usage_analytics())
        if global_settings.providers_refresh_interval_seconds > 0:
            providers_task = asyncio.create_task(providers_cache_refresher())
        key_reset_task = asyncio.create_task(periodic_key_reset())
        auto_topup_task = asyncio.create_task(periodic_auto_topup())
        refund_sweep_task = asyncio.create_task(periodic_refund_sweep())
        routstr_fee_task = asyncio.create_task(periodic_routstr_fee_payout())

        yield

    except asyncio.CancelledError:
        # Expected during shutdown
        pass
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
        if analytics_task is not None:
            analytics_task.cancel()
        if providers_task is not None:
            providers_task.cancel()
        if models_refresh_task is not None:
            models_refresh_task.cancel()
        if model_maps_refresh_task is not None:
            model_maps_refresh_task.cancel()
        if key_reset_task is not None:
            key_reset_task.cancel()
        if auto_topup_task is not None:
            auto_topup_task.cancel()
        if refund_sweep_task is not None:
            refund_sweep_task.cancel()
        if routstr_fee_task is not None:
            routstr_fee_task.cancel()

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
            if analytics_task is not None:
                tasks_to_wait.append(analytics_task)
            if providers_task is not None:
                tasks_to_wait.append(providers_task)
            if models_refresh_task is not None:
                tasks_to_wait.append(models_refresh_task)
            if model_maps_refresh_task is not None:
                tasks_to_wait.append(model_maps_refresh_task)
            if key_reset_task is not None:
                tasks_to_wait.append(key_reset_task)
            if auto_topup_task is not None:
                tasks_to_wait.append(auto_topup_task)
            if refund_sweep_task is not None:
                tasks_to_wait.append(refund_sweep_task)
            if routstr_fee_task is not None:
                tasks_to_wait.append(routstr_fee_task)

            if tasks_to_wait:
                await asyncio.gather(*tasks_to_wait, return_exceptions=True)
            logger.info("Background tasks stopped successfully")
        except Exception as e:
            logger.error(
                "Error stopping background tasks",
                extra={"error": str(e), "error_type": type(e).__name__},
            )


class _ImmutableStaticFiles(StaticFiles):
    """Static files with long Cache-Control for content-hashed Next.js assets.

    Files under `/_next/static/` are emitted with content hashes in their
    filenames and never mutate, so we serve them with a one-year immutable
    cache header so browsers and CDNs stop revalidating on every reload.
    """

    async def get_response(self, path: str, scope: Scope) -> StarletteResponse:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable"
            )
        return response


app = FastAPI(version=__version__, lifespan=lifespan)


app.add_middleware(
    CORSMiddleware,
    allow_origins=global_settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["x-routstr-request-id", "x-cashu"],
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
        "child_key_cost_msats": global_settings.child_key_cost,
    }


@app.get("/v1/providers")
async def providers() -> RedirectResponse:
    return RedirectResponse("/v1/providers/")


UI_DIST_PATH = Path(__file__).parent.parent.parent / "ui_out"

if UI_DIST_PATH.exists() and UI_DIST_PATH.is_dir():
    logger.info(f"Serving static UI from {UI_DIST_PATH}")

    app.mount(
        "/_next",
        _ImmutableStaticFiles(directory=UI_DIST_PATH / "_next", check_dir=True),
        name="next-static",
    )

    @app.get("/", include_in_schema=False)
    async def serve_root_ui() -> FileResponse:
        return FileResponse(UI_DIST_PATH / "index.html")

    # Serve the App Router RSC payload for the home page.
    @app.get("/index.txt", include_in_schema=False)
    async def serve_root_rsc() -> FileResponse:
        return FileResponse(
            UI_DIST_PATH / "index.txt", media_type="text/x-component"
        )

    @app.get("/admin")
    async def admin_redirect() -> FileResponse:
        return FileResponse(UI_DIST_PATH / "index.html")

    @app.get("/dashboard", include_in_schema=False)
    async def serve_dashboard_ui() -> FileResponse:
        return FileResponse(UI_DIST_PATH / "index.html")

    @app.get("/login", include_in_schema=False)
    async def serve_login_ui() -> FileResponse:
        return FileResponse(UI_DIST_PATH / "login" / "index.html")

    @app.get("/login/index.txt", include_in_schema=False)
    async def serve_login_rsc() -> FileResponse:
        return FileResponse(
            UI_DIST_PATH / "login" / "index.txt", media_type="text/x-component"
        )

    @app.get("/model", include_in_schema=False)
    async def serve_models_ui() -> FileResponse:
        return FileResponse(UI_DIST_PATH / "model" / "index.html")

    @app.get("/model/index.txt", include_in_schema=False)
    async def serve_model_rsc() -> FileResponse:
        return FileResponse(
            UI_DIST_PATH / "model" / "index.txt", media_type="text/x-component"
        )

    @app.get("/providers", include_in_schema=False)
    async def serve_providers_ui() -> FileResponse:
        return FileResponse(UI_DIST_PATH / "providers" / "index.html")

    @app.get("/providers/index.txt", include_in_schema=False)
    async def serve_providers_rsc() -> FileResponse:
        return FileResponse(
            UI_DIST_PATH / "providers" / "index.txt",
            media_type="text/x-component",
        )

    @app.get("/settings", include_in_schema=False)
    async def serve_settings_ui() -> FileResponse:
        return FileResponse(UI_DIST_PATH / "settings" / "index.html")

    @app.get("/settings/index.txt", include_in_schema=False)
    async def serve_settings_rsc() -> FileResponse:
        return FileResponse(
            UI_DIST_PATH / "settings" / "index.txt",
            media_type="text/x-component",
        )

    @app.get("/transactions", include_in_schema=False)
    async def serve_transactions_ui() -> FileResponse:
        return FileResponse(UI_DIST_PATH / "transactions" / "index.html")

    @app.get("/transactions/index.txt", include_in_schema=False)
    async def serve_transactions_rsc() -> FileResponse:
        return FileResponse(
            UI_DIST_PATH / "transactions" / "index.txt",
            media_type="text/x-component",
        )

    @app.get("/balances", include_in_schema=False)
    async def serve_balances_ui() -> FileResponse:
        return FileResponse(UI_DIST_PATH / "balances" / "index.html")

    @app.get("/balances/index.txt", include_in_schema=False)
    async def serve_balances_rsc() -> FileResponse:
        return FileResponse(
            UI_DIST_PATH / "balances" / "index.txt",
            media_type="text/x-component",
        )

    @app.get("/logs", include_in_schema=False)
    async def serve_logs_ui() -> FileResponse:
        return FileResponse(UI_DIST_PATH / "logs" / "index.html")

    @app.get("/logs/index.txt", include_in_schema=False)
    async def serve_logs_rsc() -> FileResponse:
        return FileResponse(
            UI_DIST_PATH / "logs" / "index.txt", media_type="text/x-component"
        )

    @app.get("/usage", include_in_schema=False)
    async def serve_usage_ui() -> FileResponse:
        return FileResponse(UI_DIST_PATH / "usage" / "index.html")

    @app.get("/usage/index.txt", include_in_schema=False)
    async def serve_usage_rsc() -> FileResponse:
        return FileResponse(
            UI_DIST_PATH / "usage" / "index.txt", media_type="text/x-component"
        )

    @app.get("/unauthorized", include_in_schema=False)
    async def serve_unauthorized_ui() -> FileResponse:
        return FileResponse(UI_DIST_PATH / "unauthorized" / "index.html")

    @app.get("/unauthorized/index.txt", include_in_schema=False)
    async def serve_unauthorized_rsc() -> FileResponse:
        return FileResponse(
            UI_DIST_PATH / "unauthorized" / "index.txt",
            media_type="text/x-component",
        )

    @app.get("/favicon.ico", include_in_schema=False)
    async def serve_favicon() -> FileResponse:
        icon_path = UI_DIST_PATH / "icon.ico"
        if icon_path.exists():
            return FileResponse(icon_path)
        return FileResponse(UI_DIST_PATH / "favicon.ico")

    @app.get("/icon.ico", include_in_schema=False)
    async def serve_icon() -> FileResponse:
        return FileResponse(UI_DIST_PATH / "icon.ico")

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
