'use client';

import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { AppPageShell } from '@/components/app-page-shell';
import { DashboardBalanceSummary } from '@/components/dashboard-balance-summary';
import { CheatSheet } from '@/components/landing/cheat-sheet';
import { Skeleton } from '@/components/ui/skeleton';
import { fetchBtcUsdPrice, btcToSatsRate } from '@/lib/exchange-rate';
import { ConfigurationService } from '@/lib/api/services/configuration';
import { useCurrencyStore } from '@/lib/stores/currency';

export default function DashboardPage() {
  const { displayUnit } = useCurrencyStore();
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isAuthResolved, setIsAuthResolved] = useState(false);

  useEffect(() => {
    if (typeof window === 'undefined') {
      return;
    }

    const syncAuthState = (): void => {
      setIsAuthenticated(ConfigurationService.isTokenValid());
      setIsAuthResolved(true);
    };

    syncAuthState();
    window.addEventListener('storage', syncAuthState);

    return () => {
      window.removeEventListener('storage', syncAuthState);
    };
  }, []);

  const { data: btcUsdPrice } = useQuery({
    queryKey: ['btc-usd-price'],
    queryFn: fetchBtcUsdPrice,
    enabled: isAuthenticated,
    refetchInterval: 120_000,
    staleTime: 60_000,
  });

  const usdPerSat = btcUsdPrice ? btcToSatsRate(btcUsdPrice) : null;

  if (!isAuthResolved) {
    return (
      <AppPageShell contentClassName='mx-auto w-full max-w-5xl'>
        <div className='space-y-6'>
          <div className='space-y-2'>
            <Skeleton className='h-8 w-56 max-w-full sm:h-9' />
            <Skeleton className='h-4 w-80 max-w-full' />
          </div>
          <Skeleton className='h-[240px] w-full rounded-xl' />
        </div>
      </AppPageShell>
    );
  }

  if (!isAuthenticated) {
    return <CheatSheet />;
  }

  return (
    <AppPageShell contentClassName='mx-auto w-full max-w-5xl'>
      <div className='min-w-0 space-y-4 sm:space-y-6'>
        <section className='space-y-1 px-1'>
          <h1 className='text-lg font-semibold tracking-tight sm:text-2xl'>
            Dashboard
          </h1>
          <p className='text-muted-foreground text-sm leading-relaxed'>
            Node balances and wallet status.
          </p>
        </section>

        <DashboardBalanceSummary
          displayUnit={displayUnit}
          usdPerSat={usdPerSat}
        />
      </div>
    </AppPageShell>
  );
}
