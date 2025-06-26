import asyncio
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .account import wallet_router
from .cashu import check_for_refunds, close_wallet, init_wallet, periodic_payout
from .db import init_db
from .discovery import providers_router
from .models import MODELS, update_sats_pricing
from .proxy import proxy_router

__version__ = "0.0.1"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    await init_db()
    await init_wallet()
    pricing_task = asyncio.create_task(update_sats_pricing())
    refund_task = asyncio.create_task(check_for_refunds())
    payout_task = asyncio.create_task(periodic_payout())

    try:
        yield
    finally:
        refund_task.cancel()
        pricing_task.cancel()
        payout_task.cancel()
        await asyncio.gather(
            pricing_task, refund_task, payout_task, return_exceptions=True
        )
        await close_wallet()


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


@app.get("/")
async def info() -> dict:
    return {
        "name": app.title,
        "description": app.description,
        "version": __version__,
        "npub": os.environ.get("NPUB", ""),
        "mint": os.environ.get("MINT", ""),
        "http_url": os.environ.get("HTTP_URL", ""),
        "onion_url": os.environ.get("ONION_URL", ""),
        "models": MODELS,
    }


app.include_router(wallet_router)
app.include_router(providers_router)
app.include_router(proxy_router)
