import asyncio
import json

from sqlmodel import select

from ..core import get_logger
from ..core.db import (
    CashuTransaction,
    UpstreamProviderRow,
    create_session,
    store_cashu_transaction,
)
from ..wallet import release_token_reservation, send_token, token_mint_url
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
    if provider is None:
        return
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

    actual_mint_url = token_mint_url(token, mint_url)
    stored = await store_cashu_transaction(
        token=token,
        amount=amount,
        unit="sat",
        mint_url=actual_mint_url,
        typ="out",
        collected=False,
        source="auto_topup",
    )
    if not stored:
        try:
            await release_token_reservation(token)
        except Exception as error:
            logger.critical(
                "Failed to release untracked auto-topup token",
                extra={
                    "provider_id": row.id,
                    "mint_url": actual_mint_url,
                    "error": str(error),
                },
            )
        else:
            logger.warning(
                "Auto-topup token was released after persistence failed",
                extra={"provider_id": row.id, "mint_url": actual_mint_url},
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
        async with create_session() as session:
            transaction = (
                await session.exec(
                    select(CashuTransaction).where(
                        CashuTransaction.token == token,
                        CashuTransaction.type == "out",
                        CashuTransaction.source == "auto_topup",
                    )
                )
            ).first()
            if transaction is None:
                logger.critical(
                    "Completed auto top-up transaction is missing from the database",
                    extra={"provider_id": row.id, "mint_url": mint_url},
                )
            else:
                transaction.collected = True
                session.add(transaction)
                await session.commit()

        logger.info(
            "Auto top-up completed successfully",
            extra={
                "provider_id": row.id,
                "amount": amount,
                "new_balance_approx": balance + amount,
            },
        )
