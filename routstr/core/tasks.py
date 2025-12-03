import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI

from ..models.models import (
    cleanup_enabled_models_periodically,
    update_sats_pricing,
)
from ..nostr import announce_provider, providers_cache_refresher
from ..payment.price import update_prices_periodically
from ..payment.wallet import periodic_payout
from ..proxy import get_upstreams, initialize_upstreams, refresh_model_maps_periodically
from ..upstream.helpers import refresh_upstreams_models_periodically
from .db import create_session, init_db, run_migrations
from .logging import get_logger
from .settings import SettingsService
from .settings import settings as global_settings

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Extract version from app if available, or use default/log without it
    version = getattr(app, "version", "unknown")

    logger.info("Application startup initiated", extra={"version": version})

    btc_price_task = None
    pricing_task = None
    payout_task = None
    listing_task = None
    providers_task = None
    models_refresh_task = None
    models_cleanup_task = None
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

        _initialize_upstreams_task = asyncio.create_task(initialize_upstreams())

        btc_price_task = asyncio.create_task(update_prices_periodically())
        pricing_task = asyncio.create_task(update_sats_pricing())
        if global_settings.models_refresh_interval_seconds > 0:
            models_refresh_task = asyncio.create_task(
                refresh_upstreams_models_periodically(get_upstreams())
            )
        models_cleanup_task = asyncio.create_task(cleanup_enabled_models_periodically())
        model_maps_refresh_task = asyncio.create_task(refresh_model_maps_periodically())
        payout_task = asyncio.create_task(periodic_payout())
        listing_task = asyncio.create_task(announce_provider())
        providers_task = asyncio.create_task(providers_cache_refresher())

        await _initialize_upstreams_task

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
        if listing_task is not None:
            listing_task.cancel()
        if providers_task is not None:
            providers_task.cancel()
        if models_refresh_task is not None:
            models_refresh_task.cancel()
        if models_cleanup_task is not None:
            models_cleanup_task.cancel()
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
            if listing_task is not None:
                tasks_to_wait.append(listing_task)
            if providers_task is not None:
                tasks_to_wait.append(providers_task)
            if models_refresh_task is not None:
                tasks_to_wait.append(models_refresh_task)
            if models_cleanup_task is not None:
                tasks_to_wait.append(models_cleanup_task)
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
