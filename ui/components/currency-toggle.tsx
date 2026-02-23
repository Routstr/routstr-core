'use client';

import { useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ChevronsUpDownIcon, CoinsIcon } from 'lucide-react';
import { useCurrencyStore } from '@/lib/stores/currency';
import { fetchBtcUsdPrice, btcToSatsRate } from '@/lib/exchange-rate';
import type { DisplayUnit } from '@/lib/types/units';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

interface CurrencyToggleProps {
  className?: string;
  compact?: boolean;
  menuSide?: 'top' | 'right' | 'bottom' | 'left';
  menuAlign?: 'start' | 'center' | 'end';
}

const UNIT_OPTIONS: Array<{ value: DisplayUnit; label: string }> = [
  { value: 'msat', label: 'mSAT' },
  { value: 'sat', label: 'sat' },
  { value: 'usd', label: 'USD' },
];

function getLabel(unit: DisplayUnit): string {
  const option = UNIT_OPTIONS.find((item) => item.value === unit);
  return option?.label ?? unit;
}

export function CurrencyToggle({
  className,
  compact = false,
  menuSide = 'bottom',
  menuAlign = 'end',
}: CurrencyToggleProps) {
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
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant='outline'
          size='sm'
          className={cn(
            'border-border/60 bg-background/65 text-muted-foreground hover:text-foreground rounded-md',
            compact ? 'h-8 w-10 justify-center px-0' : 'h-8 justify-between gap-2',
            className
          )}
        >
          <span className='inline-flex min-w-0 items-center gap-1.5'>
            <CoinsIcon className='h-3.5 w-3.5 shrink-0' />
            {compact ? null : (
              <span className='truncate text-[11px] font-medium uppercase'>
                {getLabel(displayUnit)}
              </span>
            )}
          </span>
          {compact ? (
            <span className='sr-only'>Currency: {getLabel(displayUnit)}</span>
          ) : (
            <ChevronsUpDownIcon className='h-3.5 w-3.5 opacity-70' />
          )}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent side={menuSide} align={menuAlign}>
        <DropdownMenuRadioGroup
          value={displayUnit}
          onValueChange={(value) => {
            if (value !== 'msat' && value !== 'sat' && value !== 'usd') return;
            if (value === 'usd' && !usdPerSat) return;
            setDisplayUnit(value);
          }}
        >
          {UNIT_OPTIONS.map((option) => (
            <DropdownMenuRadioItem
              key={option.value}
              value={option.value}
              disabled={option.value === 'usd' && !usdPerSat}
              className='uppercase'
            >
              {option.label}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
