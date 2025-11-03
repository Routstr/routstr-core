export async function fetchBtcUsdPrice(): Promise<number | null> {
  const sources = [
    async () => {
      const response = await fetch(
        'https://api.kraken.com/0/public/Ticker?pair=XBTUSD'
      );
      const data = await response.json();
      return parseFloat(data.result.XXBTZUSD.c[0]);
    },
    async () => {
      const response = await fetch(
        'https://api.coinbase.com/v2/prices/BTC-USD/spot'
      );
      const data = await response.json();
      return parseFloat(data.data.amount);
    },
    async () => {
      const response = await fetch(
        'https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT'
      );
      const data = await response.json();
      return parseFloat(data.price);
    },
  ];

  const results = await Promise.allSettled(
    sources.map((fn) =>
      Promise.race([
        fn(),
        new Promise<never>((_, reject) =>
          setTimeout(() => reject(new Error('Timeout')), 10000)
        ),
      ])
    )
  );

  const prices: number[] = [];
  for (const result of results) {
    if (result.status === 'fulfilled' && typeof result.value === 'number') {
      prices.push(result.value);
    }
  }

  if (prices.length === 0) {
    return null;
  }

  return Math.min(...prices);
}

export function btcToSatsRate(btcUsdPrice: number): number {
  return btcUsdPrice / 100_000_000;
}

