import os
import httpx
import asyncio
import logging

logger = logging.getLogger(__name__)
    price = float((await client.get(api)).json()["result"]["XXBTZUSD"]["c"][0])
    logger.debug("Kraken price %s", price)
    return price

    price = float((await client.get(api)).json()["data"]["amount"])
    logger.debug("Coinbase price %s", price)
    return price
    price = float((await client.get(api)).json()["price"])
    logger.debug("Binance price %s", price)
    return price


        prices = await asyncio.gather(
            kraken_btc_usd(client),
            coinbase_btc_usd(client),
            binance_btc_usdt(client),
        best_price = max(prices)
        ask = best_price * EXCHANGE_FEE
        logger.debug("Best BTC price %s, ask %s", best_price, ask)
        return ask
    price = (await btc_usd_ask_price()) / 100_000_000
    logger.debug("Sats/USD ask price %s", price)
    return price
    api = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
    try:
        return float((await client.get(api)).json()["data"]["amount"])
    except (httpx.RequestError, KeyError) as e:
        logging.warning(f"Coinbase API error: {e}")
        return None


async def binance_btc_usdt(client: httpx.AsyncClient) -> float | None:
    api = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
    try:
        return float((await client.get(api)).json()["price"])
    except (httpx.RequestError, KeyError) as e:
        logging.warning(f"Binance API error: {e}")
        return None


async def btc_usd_ask_price() -> float:
    async with httpx.AsyncClient() as client:
        return (
            max(
                [
                    price
                    for price in await asyncio.gather(
                        kraken_btc_usd(client),
                        coinbase_btc_usd(client),
                        binance_btc_usdt(client),
                    )
                    if price is not None
                ]
            )
            * EXCHANGE_FEE
        )


async def sats_usd_ask_price() -> float:
    return (await btc_usd_ask_price()) / 100_000_000
