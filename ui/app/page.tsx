'use client';

import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { AppSidebar } from '@/components/app-sidebar';
import { SiteHeader } from '@/components/site-header';
import { SidebarInset, SidebarProvider } from '@/components/ui/sidebar';
import { DetailedWalletBalance } from '@/components/detailed-wallet-balance';
import { TemporaryBalances } from '@/components/temporary-balances';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';
import type { DisplayUnit } from '@/lib/types/units';
import { fetchBtcUsdPrice, btcToSatsRate } from '@/lib/exchange-rate';

export default function Page() {
  const [displayUnit, setDisplayUnit] = useState<DisplayUnit>('sat');

  const { data: btcUsdPrice } = useQuery({
    queryKey: ['btc-usd-price'],
    queryFn: fetchBtcUsdPrice,
    refetchInterval: 120_000,
    staleTime: 60_000,
  });

  const usdPerSat = btcUsdPrice ? btcToSatsRate(btcUsdPrice) : null;

  useEffect(() => {
    if (displayUnit === 'usd' && usdPerSat === null) {
      setDisplayUnit('sat');
    }
  }, [displayUnit, usdPerSat]);

  return (
    <SidebarProvider>
      <AppSidebar variant='inset' />
      <SidebarInset className='p-0'>
        <SiteHeader />
        <div className='container max-w-6xl px-4 py-8 md:px-6 lg:px-8'>
          <div className='mb-8 flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between'>
            <div>
              <h1 className='text-3xl font-bold tracking-tight'>
                Admin Dashboard
              </h1>
              <p className='text-muted-foreground mt-2'>
                Monitor and manage wallet balances
              </p>
            </div>
            <div className='flex items-center'>
              <ToggleGroup
                type='single'
                value={displayUnit}
                onValueChange={(value) => {
                  if (value) {
                    setDisplayUnit(value as DisplayUnit);
                  }
                }}
                variant='outline'
                size='sm'
              >
                <ToggleGroupItem value='msat'>mSAT</ToggleGroupItem>
                <ToggleGroupItem value='sat'>sat</ToggleGroupItem>
                <ToggleGroupItem value='usd' disabled={!usdPerSat}>
                  USD
                </ToggleGroupItem>
              </ToggleGroup>
            </div>
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
