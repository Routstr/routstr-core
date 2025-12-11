'use client';

import { useCurrencyStore } from '@/lib/stores/currency';
import { useQuery } from '@tanstack/react-query';
import { AppSidebar } from '@/components/app-sidebar';
import { SiteHeader } from '@/components/site-header';
import { SidebarInset, SidebarProvider } from '@/components/ui/sidebar';
import { DetailedWalletBalance } from '@/components/detailed-wallet-balance';
import { TemporaryBalances } from '@/components/temporary-balances';
import { fetchBtcUsdPrice, btcToSatsRate } from '@/lib/exchange-rate';

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
    <SidebarProvider>
      <AppSidebar variant='inset' />
      <SidebarInset className='p-0'>
        <SiteHeader />
        <div className='container max-w-6xl px-4 py-8 md:px-6 lg:px-8'>
          <div className='mb-8 flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between'>
            <div>
              <h1 className='text-3xl font-bold tracking-tight'>Balances</h1>
              <p className='text-muted-foreground mt-2'>
                Monitor and manage wallet balances
              </p>
            </div>
            {/* Global currency toggle is now in SiteHeader */}
          </div>

          <div className='grid gap-6'>
            <div className='col-span-full'>
              <DetailedWalletBalance
                refreshInterval={30000}
                displayUnit={displayUnit}
                usdPerSat={usdPerSat}
              />
            </div>
            <div className='col-span-full'>
              <TemporaryBalances
                refreshInterval={60000}
                displayUnit={displayUnit}
                usdPerSat={usdPerSat}
              />
            </div>
          </div>
        </div>
      </SidebarInset>
    </SidebarProvider>
  );
}
