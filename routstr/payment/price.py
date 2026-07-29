import asyncio
import random

import httpx

from ..core import get_logger
from ..core.settings import settings

logger = get_logger(__name__)

BTC_USD_PRICE: float | None = None
SATS_USD_PRICE: float | None = None


def _parse_quote(raw: object, exchange: str) -> float | None:
    """Coerce an exchange quote to a price, or ``None`` if it is not one.

    Every quote passes through here because the aggregator takes the ``min()``
    of what it collects: an unusable quote does not merely join the sample, it
    *wins* it, and the result is the rate every model and every request on the
    node is priced at. A zero divides by zero on the USD cost path, ``NaN``
    raises out of the integer conversion in settlement, and a negative rate
    produces a negative charge that is credited back to the caller.

    ``is_usable_rate`` is the same predicate the billable-rate guards use, so
    "finite and non-negative" has one definition; a quote is stricter still and
    must be positive, since a BTC price of zero is a broken feed, not free money.
    Imported lazily because ``payment.models`` imports this module.
    """
    from .models import is_usable_rate

    # A boolean is a shape change, not a price: `float(True)` is a finite,
    # positive 1.0 that passes every numeric guard below and then wins the
    # `min()`, pricing the node at one dollar per bitcoin.
    if isinstance(raw, bool):
        logger.warning(
            "Non-numeric price quote — ignoring this exchange",
            extra={"exchange": exchange, "quote": repr(raw)},
        )
        return None

    try:
        price = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as e:
        logger.warning(
            "Unparseable price quote — ignoring this exchange",
            extra={
                "error": str(e),
                "error_type": type(e).__name__,
                "exchange": exchange,
                "quote": repr(raw),
            },
        )
        return None

    if not is_usable_rate(price) or price <= 0:
        logger.warning(
            "Unusable price quote — ignoring this exchange",
            extra={"exchange": exchange, "quote": price},
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
    SATS_USD_PRICE = btc_price / 100_000_000


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
