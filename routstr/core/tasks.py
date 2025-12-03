import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI

from ..balance import balance_router, deprecated_wallet_router
from ..discovery import providers_cache_refresher
from ..nip91 import announce_provider
from ..payment.models import (
    cleanup_enabled_models_periodically,
    update_sats_pricing,
)
from ..payment.price import _update_prices, update_prices_periodically
from ..proxy import get_upstreams, initialize_upstreams, refresh_model_maps_periodically
from ..upstream.helpers import refresh_upstreams_models_periodically
from ..wallet import periodic_payout
from .db import create_session, init_db, run_migrations
from .logging import get_logger
from .settings import SettingsService
from .settings import settings as global_settings

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    version = getattr(app, "version", "unknown")

    logger.info("Application startup initiated", extra={"version": version})

    btc_price_task: asyncio.Task | None = None
    pricing_task: asyncio.Task | None = None
    payout_task: asyncio.Task | None = None
    nip91_task: asyncio.Task | None = None
    providers_task: asyncio.Task | None = None
    models_refresh_task: asyncio.Task | None = None
    models_cleanup_task: asyncio.Task | None = None
    model_maps_refresh_task: asyncio.Task | None = None

    try:
        logger.info("Running database migrations")
        run_migrations()

        await init_db()

        async with create_session() as session:
            s = await SettingsService.initialize(session)

        try:
            app.title = s.name
            app.description = s.description
        except Exception:  # pragma: no cover - defensive
            pass

        await _update_prices()
        await initialize_upstreams()

        btc_price_task = asyncio.create_task(update_prices_periodically())
        pricing_task = asyncio.create_task(update_sats_pricing())
        if global_settings.models_refresh_interval_seconds > 0:
            models_refresh_task = asyncio.create_task(
                refresh_upstreams_models_periodically(get_upstreams())
            )
        models_cleanup_task = asyncio.create_task(cleanup_enabled_models_periodically())
        model_maps_refresh_task = asyncio.create_task(refresh_model_maps_periodically())
        payout_task = asyncio.create_task(periodic_payout())
        if global_settings.nsec:
            nip91_task = asyncio.create_task(announce_provider())
        if global_settings.providers_refresh_interval_seconds > 0:
            providers_task = asyncio.create_task(providers_cache_refresher())

        yield

    except Exception as e:  # pragma: no cover - logged and re-raised
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
        if models_cleanup_task is not None:
            models_cleanup_task.cancel()
        if model_maps_refresh_task is not None:
            model_maps_refresh_task.cancel()

        try:
            tasks_to_wait: list[asyncio.Task] = []
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
            if models_cleanup_task is not None:
                tasks_to_wait.append(models_cleanup_task)
            if model_maps_refresh_task is not None:
                tasks_to_wait.append(model_maps_refresh_task)

            if tasks_to_wait:
                await asyncio.gather(*tasks_to_wait, return_exceptions=True)
            logger.info("Background tasks stopped successfully")
        except Exception as e:  # pragma: no cover - defensive
            logger.error(
                "Error stopping background tasks",
                extra={"error": str(e), "error_type": type(e).__name__},
            )
