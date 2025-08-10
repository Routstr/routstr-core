import asyncio
import os
from datetime import datetime

import httpx

from ..core.logging import get_logger
from ..core.settings import SettingsManager

logger = get_logger(__name__)

# Default values - will be loaded from settings
_EXCHANGE_FEE = None
_UPSTREAM_PROVIDER_FEE = None


async def _get_exchange_fee() -> float:
    """Get exchange fee from settings."""
    global _EXCHANGE_FEE
    if _EXCHANGE_FEE is None:
        _EXCHANGE_FEE = await SettingsManager.get("EXCHANGE_FEE", 1.005)
    return _EXCHANGE_FEE


async def _get_upstream_provider_fee() -> float:
    """Get upstream provider fee from settings."""
    global _UPSTREAM_PROVIDER_FEE
    if _UPSTREAM_PROVIDER_FEE is None:
        _UPSTREAM_PROVIDER_FEE = await SettingsManager.get("UPSTREAM_PROVIDER_FEE", 1.05)
    return _UPSTREAM_PROVIDER_FEE


# Cache reload function
async def reload_fee_settings() -> None:
    """Reload fee settings from database."""
    global _EXCHANGE_FEE, _UPSTREAM_PROVIDER_FEE
    _EXCHANGE_FEE = None
    _UPSTREAM_PROVIDER_FEE = None


async def kraken_btc_usd(client: httpx.AsyncClient) -> float | None:
    """Fetch BTC/USD price from Kraken API."""
    api = "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"
    try:
        response = await client.get(api)
        price_data = response.json()
        price = float(price_data["result"]["XXBTZUSD"]["c"][0])

        return price
    except (httpx.RequestError, KeyError) as e:
        logger.warning(
            "Kraken API error",
            extra={
                "error": str(e),
                "error_type": type(e).__name__,
                "exchange": "kraken",
            },
        )
        return None


async def coinbase_btc_usd(client: httpx.AsyncClient) -> float | None:
    """Fetch BTC/USD price from Coinbase API."""
    api = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
    try:
        response = await client.get(api)
        price_data = response.json()
        price = float(price_data["data"]["amount"])

        return price
    except (httpx.RequestError, KeyError) as e:
        logger.warning(
            "Coinbase API error",
            extra={
                "error": str(e),
                "error_type": type(e).__name__,
                "exchange": "coinbase",
            },
        )
        return None


async def binance_btc_usdt(client: httpx.AsyncClient) -> float | None:
    """Fetch BTC/USDT price from Binance API."""
    api = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
    try:
        response = await client.get(api)
        price_data = response.json()
        price = float(price_data["price"])

        return price
    except (httpx.RequestError, KeyError) as e:
        logger.warning(
            "Binance API error",
            extra={
                "error": str(e),
                "error_type": type(e).__name__,
                "exchange": "binance",
            },
        )
        return None


async def btc_usd_ask_price() -> float:
    """Get the highest BTC/USD price from multiple exchanges with fee adjustment."""

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            prices = await asyncio.gather(
                kraken_btc_usd(client),
                coinbase_btc_usd(client),
                binance_btc_usdt(client),
            )

            valid_prices = [price for price in prices if price is not None]

            if not valid_prices:
                logger.error("No valid BTC prices obtained from any exchange")
                raise ValueError("Unable to fetch BTC price from any exchange")

            max_price = max(valid_prices)
            exchange_fee = await _get_exchange_fee()
            upstream_provider_fee = await _get_upstream_provider_fee()
            final_price = max_price * exchange_fee * upstream_provider_fee

            return final_price

        except Exception as e:
            logger.error(
                "Error in BTC price aggregation",
                extra={"error": str(e), "error_type": type(e).__name__},
            )
            raise


async def sats_usd_ask_price() -> float:
    """Get the USD price per satoshi."""

    try:
        btc_price = await btc_usd_ask_price()
        sats_price = btc_price / 100_000_000

        return sats_price

    except Exception as e:
        logger.error(
            "Error calculating satoshi price",
            extra={"error": str(e), "error_type": type(e).__name__},
        )
        raise
