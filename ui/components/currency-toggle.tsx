'use client';

import { useCurrencyStore } from '@/lib/stores/currency';
import { useQuery } from '@tanstack/react-query';
import { fetchBtcUsdPrice, btcToSatsRate } from '@/lib/exchange-rate';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';
import { useEffect } from 'react';
import type { DisplayUnit } from '@/lib/types/units';

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

  return (
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
      className="mr-2"
    >
      <ToggleGroupItem value='msat' aria-label="Toggle msat" title="Millisatoshis">
        mSAT
      </ToggleGroupItem>
      <ToggleGroupItem value='sat' aria-label="Toggle sat" title="Satoshis">
        sat
      </ToggleGroupItem>
      <ToggleGroupItem 
        value='usd' 
        disabled={!usdPerSat} 
        aria-label="Toggle usd" 
        title="US Dollar (approx)"
      >
        USD
      </ToggleGroupItem>
    </ToggleGroup>
  );
}

