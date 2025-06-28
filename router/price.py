import asyncio
import logging
import os

import httpx

# artifical spread to cover conversion fees
EXCHANGE_FEE = float(os.environ.get("EXCHANGE_FEE", "1.005"))  # 0.5% default


async def kraken_btc_usd(client: httpx.AsyncClient) -> float | None:
    api = "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"
    try:
        return float((await client.get(api)).json()["result"]["XXBTZUSD"]["c"][0])
    except (httpx.RequestError, KeyError) as e:
        logging.warning(f"Kraken API error: {e}")
        return None


async def coinbase_btc_usd(client: httpx.AsyncClient) -> float | None:
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
