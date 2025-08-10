import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..balance import balance_router, deprecated_wallet_router
from ..discovery import providers_router
from ..payment.models import MODELS, models_router, update_sats_pricing
from ..proxy import proxy_router
from ..wallet import periodic_payout
from .admin import admin_router
from .db import init_db, run_migrations
from .logging import get_logger, setup_logging
from .settings import SettingsManager

# Initialize logging first
setup_logging()
logger = get_logger(__name__)

__version__ = "0.1.0"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Application startup initiated", extra={"version": __version__})

    pricing_task = None
    payout_task = None

    try:
        # Run database migrations on startup
        # This ensures the database schema is always up-to-date in production
        # Migrations are idempotent - running them multiple times is safe
        logger.info("Running database migrations")
        run_migrations()

        # Initialize database connection pools
        # This creates any tables that might not be tracked by migrations yet
        await init_db()

        # Initialize settings from environment variables
        logger.info("Initializing settings manager")
        await SettingsManager.initialize()

        pricing_task = asyncio.create_task(update_sats_pricing())
        payout_task = asyncio.create_task(periodic_payout())

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

        try:
            tasks_to_wait = []
            if pricing_task is not None:
                tasks_to_wait.append(pricing_task)
            if payout_task is not None:
                tasks_to_wait.append(payout_task)

            if tasks_to_wait:
                await asyncio.gather(*tasks_to_wait, return_exceptions=True)
            logger.info("Background tasks stopped successfully")
        except Exception as e:
            logger.error(
                "Error stopping background tasks",
                extra={"error": str(e), "error_type": type(e).__name__},
            )


app = FastAPI(
    version=__version__,
    title="ARoutstrNode" + __version__,  # Default title
    description="A Routstr Node",  # Default description
    contact={"name": "", "npub": ""},  # Default contact
    lifespan=lifespan,
)

# Middleware configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Default, will be updated from settings
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
@app.get("/v1/info")
async def get_info() -> dict[str, str | list[str]]:
    """Get basic node information with settings from database."""
    # Get settings from database
    name = await SettingsManager.get("NAME", "ARoutstrNode" + __version__)
    description = await SettingsManager.get("DESCRIPTION", "A Routstr Node")
    npub = await SettingsManager.get("NPUB", "")
    cashu_mints = await SettingsManager.get(
        "CASHU_MINTS", "https://mint.minibits.cash/Bitcoin"
    )
    http_url = await SettingsManager.get("HTTP_URL", "")
    onion_url = await SettingsManager.get("ONION_URL", "")

    # Split cashu_mints by comma
    mints_list = [mint.strip() for mint in cashu_mints.split(",") if mint.strip()]

    return {
        "name": name,
        "description": description,
        "version": __version__,
        "npub": npub,
        "mints": mints_list,
        "http_url": http_url,
        "onion_url": onion_url,
        "models": [model.id for model in MODELS],
    }


app.include_router(models_router)
app.include_router(admin_router)
app.include_router(balance_router)
app.include_router(deprecated_wallet_router)
app.include_router(providers_router)
app.include_router(proxy_router)
