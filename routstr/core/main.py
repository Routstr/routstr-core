import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from starlette.exceptions import HTTPException

from ..balance import balance_router, deprecated_wallet_router
from ..discovery import providers_router
from ..payment.models import models_router
from ..proxy import proxy_router
from .admin import admin_router
from .exceptions import general_exception_handler, http_exception_handler
from .logging import get_logger, setup_logging
from .middleware import LoggingMiddleware
from .settings import settings as global_settings
from .tasks import lifespan
from .ui import setup_ui_routes

# Initialize logging first
setup_logging()
logger = get_logger(__name__)

if os.getenv("VERSION_SUFFIX") is not None:
    __version__ = f"0.2.1-{os.getenv('VERSION_SUFFIX')}"
else:
    __version__ = "0.2.1"


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
    }


@app.get("/v1/providers")
async def providers() -> RedirectResponse:
    return RedirectResponse("/v1/providers/")


setup_ui_routes(app)

app.include_router(models_router)
app.include_router(admin_router)
app.include_router(balance_router)
app.include_router(deprecated_wallet_router)
app.include_router(providers_router)
app.include_router(proxy_router)
