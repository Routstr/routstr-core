import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from starlette.exceptions import HTTPException

from ..balance import balance_router, deprecated_wallet_router
from ..discovery import providers_cache_refresher, providers_router
from ..nip91 import announce_provider
from ..payment.models import (
    ensure_models_bootstrapped,
    models_router,
    refresh_models_periodically,
    update_sats_pricing,
)
from ..proxy import proxy_router
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

__version__ = "0.1.3"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Application startup initiated", extra={"version": __version__})

    pricing_task = None
    payout_task = None
    nip91_task = None
    providers_task = None
    models_refresh_task = None

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

        await ensure_models_bootstrapped()
        pricing_task = asyncio.create_task(update_sats_pricing())
        if global_settings.models_refresh_interval_seconds > 0:
            models_refresh_task = asyncio.create_task(refresh_models_periodically())
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

        try:
            tasks_to_wait = []
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


@app.get("/", include_in_schema=False)
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
    }


@app.get("/admin")
async def admin_redirect() -> RedirectResponse:
    return RedirectResponse("/admin/")


app.include_router(models_router)
app.include_router(admin_router)
app.include_router(balance_router)
app.include_router(deprecated_wallet_router)
app.include_router(providers_router)
app.include_router(proxy_router)