import asyncio
import json

from sqlmodel import select

from ..core import get_logger
from ..core.db import UpstreamProviderRow, create_session
from ..wallet import send_token
from .routstr import RoutstrUpstreamProvider

logger = get_logger(__name__)

# Check every 60 seconds
AUTO_TOPUP_INTERVAL_SECONDS = 60


async def periodic_auto_topup() -> None:
    """Background task that monitors Routstr provider balances and auto-tops up when below threshold.

    For each Routstr provider with auto_topup enabled in provider_settings:
    1. Checks the upstream balance via get_balance()
    2. If balance < topup_threshold, creates a cashu token from the configured mint
    3. Sends the token to the upstream provider via topup()
    """
    # Wait for initial startup to complete
    await asyncio.sleep(30)
    logger.info("Auto top-up worker started")

    while True:
        try:
            await _run_auto_topup_cycle()
        except Exception as e:
            logger.error(
                "Auto top-up cycle failed",
                extra={"error": str(e), "error_type": type(e).__name__},
            )

        await asyncio.sleep(AUTO_TOPUP_INTERVAL_SECONDS)


async def _run_auto_topup_cycle() -> None:
    """Single cycle: check all eligible providers and top up if needed."""
    async with create_session() as session:
        query = select(UpstreamProviderRow).where(
            UpstreamProviderRow.provider_type == "routstr",
            UpstreamProviderRow.enabled == True,  # noqa: E712
        )
        result = await session.exec(query)
        providers = result.all()

    for row in providers:
        try:
            await _check_and_topup(row)
        except Exception as e:
            logger.error(
                "Auto top-up failed for provider",
                extra={
                    "provider_id": row.id,
                    "base_url": row.base_url,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )


async def _check_and_topup(row: UpstreamProviderRow) -> None:
    """Check a single provider's balance and top up if below threshold."""
    # Parse provider settings
    settings: dict = {}
    if row.provider_settings:
        try:
            settings = json.loads(row.provider_settings)
        except (json.JSONDecodeError, TypeError):
            return

    if not settings.get("auto_topup"):
        return

    threshold = settings.get("topup_threshold")
    amount = settings.get("topup_amount_limit")
    mint_url = settings.get("topup_mint_url")

    if not threshold or not amount or not mint_url:
        logger.warning(
            "Auto top-up enabled but missing configuration",
            extra={
                "provider_id": row.id,
                "has_threshold": bool(threshold),
                "has_amount": bool(amount),
                "has_mint": bool(mint_url),
            },
        )
        return

    if not row.api_key:
        return

    # Instantiate provider and check balance
    provider = RoutstrUpstreamProvider.from_db_row(row)
    balance = await provider.get_balance()

    if balance is None:
        logger.warning(
            "Could not fetch balance for auto top-up",
            extra={"provider_id": row.id, "base_url": row.base_url},
        )
        return

    if balance >= threshold * 1000:
        return

    # Balance is below threshold - create token and top up
    logger.info(
        "Auto top-up triggered",
        extra={
            "provider_id": row.id,
            "balance": balance,
            "threshold": threshold,
            "topup_amount": amount,
            "mint_url": mint_url,
        },
    )

    print(amount, mint_url)
    try:
        token = await send_token(amount, "sat", mint_url)
    except Exception as e:
        logger.error(
            "Failed to create cashu token for auto top-up",
            extra={
                "provider_id": row.id,
                "amount": amount,
                "mint_url": mint_url,
                "error": str(e),
            },
        )
        return

    result = await provider.topup(token)

    if "error" in result:
        logger.error(
            "Auto top-up upstream call failed",
            extra={
                "provider_id": row.id,
                "error": result["error"],
            },
        )
    else:
        logger.info(
            "Auto top-up completed successfully",
            extra={
                "provider_id": row.id,
                "amount": amount,
                "new_balance_approx": balance + amount,
            },
        )
