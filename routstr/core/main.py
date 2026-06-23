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

from ..auth import periodic_key_reset, periodic_stale_reservation_sweep
from ..balance import balance_router, deprecated_wallet_router
from ..lightning import lightning_router, periodic_invoice_watcher
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
from ..upstream.litellm_routing import configure_litellm
from ..wallet import periodic_payout, periodic_refund_sweep, periodic_routstr_fee_payout
from .admin import admin_router
from .db import create_session, init_db, run_migrations
from .exceptions import general_exception_handler, http_exception_handler
from .logging import get_logger, setup_logging
from .middleware import LoggingMiddleware
from .not_found import _NOT_FOUND_HTML, not_found_catch_all  # noqa: F401
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
    stale_reservation_task = None
    auto_topup_task = None
    refund_sweep_task = None
    routstr_fee_task = None
    invoice_watcher_task = None

    try:
        # Apply litellm-wide settings (drop_params, chat-completions URL,
        # debug logging) before any upstream provider dispatches a request.
        configure_litellm()

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
        stale_reservation_task = asyncio.create_task(
            periodic_stale_reservation_sweep()
        )
        auto_topup_task = asyncio.create_task(periodic_auto_topup())
        refund_sweep_task = asyncio.create_task(periodic_refund_sweep())
        routstr_fee_task = asyncio.create_task(periodic_routstr_fee_payout())
        invoice_watcher_task = asyncio.create_task(periodic_invoice_watcher())

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
        if stale_reservation_task is not None:
            stale_reservation_task.cancel()
        if auto_topup_task is not None:
            auto_topup_task.cancel()
        if refund_sweep_task is not None:
            refund_sweep_task.cancel()
        if routstr_fee_task is not None:
            routstr_fee_task.cancel()
        if invoice_watcher_task is not None:
            invoice_watcher_task.cancel()

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
            if stale_reservation_task is not None:
                tasks_to_wait.append(stale_reservation_task)
            if auto_topup_task is not None:
                tasks_to_wait.append(auto_topup_task)
            if refund_sweep_task is not None:
                tasks_to_wait.append(refund_sweep_task)
            if routstr_fee_task is not None:
                tasks_to_wait.append(routstr_fee_task)
            if invoice_watcher_task is not None:
                tasks_to_wait.append(invoice_watcher_task)

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
    expose_headers=[
        "x-routstr-request-id",
        "x-cashu",
        "x-routstr-cost-msats",
        "x-routstr-cost-usd",
        "x-routstr-input-cost-msats",
        "x-routstr-output-cost-msats",
    ],
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

    # Next.js is built with `trailingSlash: true`, so all UI page URLs end
    # with a slash (e.g. `/login/`). The proxy router catches `/{path:path}`
    # before FastAPI's `redirect_slashes` logic can normalize the URL, so we
    # must register both the with-slash and without-slash variants here.
    UI_PAGES = (
        "dashboard",
        "login",
        "model",
        "providers",
        "settings",
        "transactions",
        "balances",
        "logs",
        "usage",
        "unauthorized",
    )

    def _register_ui_page(name: str) -> None:
        page_dir = UI_DIST_PATH / name
        index_html = page_dir / "index.html"
        index_txt = page_dir / "index.txt"

        async def serve_page() -> FileResponse:
            return FileResponse(index_html)

        async def serve_page_rsc() -> FileResponse:
            return FileResponse(index_txt, media_type="text/x-component")

        app.add_api_route(
            f"/{name}",
            serve_page,
            methods=["GET"],
            include_in_schema=False,
            name=f"serve_{name}_ui",
        )
        app.add_api_route(
            f"/{name}/",
            serve_page,
            methods=["GET"],
            include_in_schema=False,
            name=f"serve_{name}_ui_slash",
        )
        app.add_api_route(
            f"/{name}/index.txt",
            serve_page_rsc,
            methods=["GET"],
            include_in_schema=False,
            name=f"serve_{name}_rsc",
        )

    for _page in UI_PAGES:
        _register_ui_page(_page)

    @app.get("/admin")
    async def admin_redirect() -> FileResponse:
        return FileResponse(UI_DIST_PATH / "index.html")

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
app.include_router(lightning_router)
app.include_router(deprecated_wallet_router)
app.include_router(providers_router)
app.include_router(proxy_router)
