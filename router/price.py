import httpx
import asyncio

# artifical spread to cover conversion fees
from .settings import EXCHANGE_FEE


async def kraken_btc_usd(client: httpx.AsyncClient) -> float:
    api = "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"
    return float((await client.get(api)).json()["result"]["XXBTZUSD"]["c"][0])


async def coinbase_btc_usd(client: httpx.AsyncClient) -> float:
    api = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
    return float((await client.get(api)).json()["data"]["amount"])


async def binance_btc_usdt(client: httpx.AsyncClient) -> float:
    api = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
    return float((await client.get(api)).json()["price"])


async def btc_usd_ask_price() -> float:
    async with httpx.AsyncClient() as client:
        return (
            max(
                await asyncio.gather(
                    kraken_btc_usd(client),
                    coinbase_btc_usd(client),
                    binance_btc_usdt(client),
                )
            )
            * EXCHANGE_FEE
        )


async def sats_usd_ask_price() -> float:
    return (await btc_usd_ask_price()) / 100_000_000
