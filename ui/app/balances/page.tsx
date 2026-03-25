'use client';

import { useCurrencyStore } from '@/lib/stores/currency';
import { useQuery } from '@tanstack/react-query';
import { DetailedWalletBalance } from '@/components/detailed-wallet-balance';
import { TemporaryBalances } from '@/components/temporary-balances';
import { fetchBtcUsdPrice, btcToSatsRate } from '@/lib/exchange-rate';
import { AppPageShell } from '@/components/app-page-shell';
import { PageHeader } from '@/components/page-header';

export default function BalancesPage() {
  const { displayUnit } = useCurrencyStore();

  const { data: btcUsdPrice } = useQuery({
    queryKey: ['btc-usd-price'],
    queryFn: fetchBtcUsdPrice,
    refetchInterval: 120_000,
    staleTime: 60_000,
  });

  const usdPerSat = btcUsdPrice ? btcToSatsRate(btcUsdPrice) : null;

  return (
    <AppPageShell contentClassName='mx-auto w-full max-w-5xl'>
      <div className='space-y-6'>
        <PageHeader
          title='Balances'
          description='Monitor and manage wallet balances across cashu mints and temporary stores.'
        />

        <div className='grid gap-6'>
          <DetailedWalletBalance
            refreshInterval={30000}
            displayUnit={displayUnit}
            usdPerSat={usdPerSat}
          />
          <TemporaryBalances
            refreshInterval={60000}
            displayUnit={displayUnit}
            usdPerSat={usdPerSat}
          />
        </div>
      </div>
    </AppPageShell>
  );
}
