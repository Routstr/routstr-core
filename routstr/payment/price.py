import asyncio
import random

import httpx

from ..core import get_logger
from ..core.settings import settings

logger = get_logger(__name__)

BTC_USD_PRICE: float | None = None
SATS_USD_PRICE: float | None = None


async def _kraken_btc_usd(client: httpx.AsyncClient) -> float | None:
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


async def _coinbase_btc_usd(client: httpx.AsyncClient) -> float | None:
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


async def _binance_btc_usdt(client: httpx.AsyncClient) -> float | None:
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


async def _fetch_btc_usd_price() -> float:
    """Fetch the lowest BTC/USD price from multiple exchanges."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            prices = await asyncio.gather(
                _kraken_btc_usd(client),
                _coinbase_btc_usd(client),
                _binance_btc_usdt(client),
            )
            valid_prices = [price for price in prices if price is not None]
            if not valid_prices:
                logger.error("No valid BTC prices obtained from any exchange")
                raise ValueError("Unable to fetch BTC price from any exchange")
            return min(valid_prices)
        except Exception as e:
            logger.error(
                "Error in BTC price aggregation",
                extra={"error": str(e), "error_type": type(e).__name__},
            )
            raise


async def _update_prices() -> None:
    """Update global BTC and SATS price variables."""
    global BTC_USD_PRICE, SATS_USD_PRICE
    try:
        btc_price = await _fetch_btc_usd_price()
    except Exception as e:
        logger.warning(
            "Skipping price update; unable to fetch BTC price",
            extra={"error": str(e), "error_type": type(e).__name__},
        )
        return
    BTC_USD_PRICE = btc_price
    SATS_USD_PRICE = btc_price / 100_000_000
    logger.info(
        "Updated BTC/USD price",
        extra={"btc_usd": btc_price, "sats_usd": SATS_USD_PRICE},
    )


def btc_usd_price() -> float:
    """Get the current BTC/USD price."""
    if BTC_USD_PRICE is None:
        raise ValueError("BTC price not initialized")
    return BTC_USD_PRICE


def sats_usd_price() -> float:
    """Get the current USD price per satoshi."""
    if SATS_USD_PRICE is None:
        raise ValueError("SATS price not initialized")
    return SATS_USD_PRICE


async def update_prices_periodically() -> None:
    """Background task to periodically update BTC and SATS prices."""
    try:
        if not settings.enable_pricing_refresh:
            return
    except Exception:
        pass

    await _update_prices()

    while True:
        try:
            interval = getattr(settings, "pricing_refresh_interval_seconds", 120)
            jitter = max(0.0, float(interval) * 0.1)
            await asyncio.sleep(interval + random.uniform(0, jitter))
        except asyncio.CancelledError:
            break

        try:
            if not settings.enable_pricing_refresh:
                return
        except Exception:
            pass

        try:
            await _update_prices()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error updating BTC/SATS prices: {e}")
