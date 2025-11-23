'use client';

import { useCurrencyStore } from '@/lib/stores/currency';
import { useQuery } from '@tanstack/react-query';
import { fetchBtcUsdPrice, btcToSatsRate } from '@/lib/exchange-rate';
import { useEffect } from 'react';
import type { DisplayUnit } from '@/lib/types/units';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Button } from '@/components/ui/button';
import { Coins } from 'lucide-react';

export function CurrencyToggle() {
  const { displayUnit, setDisplayUnit } = useCurrencyStore();

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
  }, [displayUnit, usdPerSat, setDisplayUnit]);

  const getLabel = (unit: DisplayUnit) => {
    switch (unit) {
      case 'msat':
        return 'mSAT';
      case 'sat':
        return 'sat';
      case 'usd':
        return 'USD';
      default:
        return unit;
    }
  };

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="sm" className="h-9 w-9 px-0 gap-2 w-auto px-3 font-normal">
          <Coins className="h-4 w-4" />
          <span className="hidden sm:inline-block">{getLabel(displayUnit)}</span>
          <span className="sm:hidden uppercase">{displayUnit}</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem onClick={() => setDisplayUnit('msat')}>
          Millisatoshis (mSAT)
        </DropdownMenuItem>
        <DropdownMenuItem onClick={() => setDisplayUnit('sat')}>
          Satoshis (sat)
        </DropdownMenuItem>
        <DropdownMenuItem 
          onClick={() => setDisplayUnit('usd')}
          disabled={!usdPerSat}
        >
          US Dollar (USD)
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

