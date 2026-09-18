import asyncio
import random

import httpx

from ..core import get_logger
from ..core.settings import settings
from .rates import coerce_rate

logger = get_logger(__name__)

BTC_USD_PRICE: float | None = None
SATS_USD_PRICE: float | None = None

SATS_PER_BTC = 100_000_000


def _parse_quote(raw: object, exchange: str) -> float | None:
    """Coerce an exchange quote to a price, or ``None`` if it is not one.

    Every quote passes through here because the aggregator takes the ``min()``
    of what it collects: an unusable quote does not merely join the sample, it
    *wins* it, and the result is the rate every model and every request on the
    node is priced at. A quote is stricter than a billable rate — it must be
    positive, and positive *after* the sats conversion the node prices in: a
    subnormal quote survives every guard here and still underflows to a zero
    sats price, which then divides by zero on every model's rate.
    """
    price = coerce_rate(raw)
    if price is None or price <= 0 or price / SATS_PER_BTC <= 0:
        logger.warning(
            "Unusable price quote — ignoring this exchange",
            extra={"exchange": exchange, "quote": repr(raw)},
        )
        return None

    return price


async def _kraken_btc_usd(client: httpx.AsyncClient) -> float | None:
    """Fetch BTC/USD price from Kraken API."""
    api = "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"
    try:
        response = await client.get(api)
        price_data = response.json()
        return _parse_quote(price_data["result"]["XXBTZUSD"]["c"][0], "kraken")
    except (httpx.RequestError, KeyError, IndexError, TypeError, ValueError) as e:
        # A payload whose *shape* changed raises IndexError/TypeError, and a
        # non-JSON body raises ValueError; unhandled, one exchange's bad day
        # aborted the whole aggregation instead of dropping a single quote.
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
        return _parse_quote(price_data["data"]["amount"], "coinbase")
    except (httpx.RequestError, KeyError, IndexError, TypeError, ValueError) as e:
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
        return _parse_quote(price_data["price"], "binance")
    except (httpx.RequestError, KeyError, IndexError, TypeError, ValueError) as e:
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
            tasks = [
                asyncio.create_task(_kraken_btc_usd(client)),
                asyncio.create_task(_coinbase_btc_usd(client)),
                asyncio.create_task(_binance_btc_usdt(client)),
            ]
            valid_prices: list[float] = []

            for future in asyncio.as_completed(tasks):
                price = await future
                if price is not None:
                    valid_prices.append(price)

                if len(valid_prices) >= 2:
                    break

            for task in tasks:
                if not task.done():
                    task.cancel()

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
    SATS_USD_PRICE = btc_price / SATS_PER_BTC


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
