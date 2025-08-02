import asyncio
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..balance import balance_router, deprecated_wallet_router
from ..discovery import providers_router
from ..payment.models import MODELS, models_router, update_sats_pricing
from ..proxy import proxy_router
from ..wallet import check_for_refunds, periodic_payout
from .admin import admin_router
from .db import init_db
from .logging import get_logger, setup_logging

# Initialize logging first
setup_logging()
logger = get_logger(__name__)

__version__ = "0.0.1"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Application startup initiated", extra={"version": __version__})

    try:
        await init_db()

        pricing_task = asyncio.create_task(update_sats_pricing())
        refund_task = asyncio.create_task(check_for_refunds())
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

        refund_task.cancel()
        pricing_task.cancel()
        payout_task.cancel()

        try:
            await asyncio.gather(
                pricing_task, refund_task, payout_task, return_exceptions=True
            )
            logger.info("Background tasks stopped successfully")
        except Exception as e:
            logger.error(
                "Error stopping background tasks",
                extra={"error": str(e), "error_type": type(e).__name__},
            )


app = FastAPI(
    version=__version__,
    title=os.environ.get("NAME", "ARoutstrNode" + __version__),
    description=os.environ.get("DESCRIPTION", "A Routstr Node"),
    contact={"name": os.environ.get("NAME", ""), "npub": os.environ.get("NPUB", "")},
    lifespan=lifespan,
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
@app.get("/v1/info")
async def info() -> dict:
    logger.info("Info endpoint accessed")
    return {
        "name": app.title,
        "description": app.description,
        "version": __version__,
        "npub": os.environ.get("NPUB", ""),
        "mints": os.environ.get("CASHU_MINTS", "").split(","),
        "http_url": os.environ.get("HTTP_URL", ""),
        "onion_url": os.environ.get("ONION_URL", ""),
        "models": MODELS,
    }


app.include_router(models_router)
app.include_router(admin_router)
app.include_router(balance_router)
app.include_router(deprecated_wallet_router)
app.include_router(providers_router)
app.include_router(proxy_router)
