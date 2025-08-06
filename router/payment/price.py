import asyncio
import os

import httpx

from ..core import get_logger

logger = get_logger(__name__)

# artifical spread to cover conversion fees
EXCHANGE_FEE = float(os.environ.get("EXCHANGE_FEE", "1.005"))  # 0.5% default
UPSTREAM_PROVIDER_FEE = float(
    os.environ.get("UPSTREAM_PROVIDER_FEE", "1.05")
)  # 5% default (e.g. openrouter charges 5% margin)


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
            final_price = max_price * EXCHANGE_FEE * UPSTREAM_PROVIDER_FEE

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
