'use client';

import { Card, CardContent, CardDescription, CardHeader } from '@/components/ui/card';

interface WalletBalanceStatsProps {
  balanceMsats?: number;
  reservedMsats?: number;
}

function formatMsats(msats: number): string {
  return new Intl.NumberFormat('en-US').format(msats);
}

function formatSats(msats: number): string {
  return new Intl.NumberFormat('en-US').format(Math.floor(msats / 1000));
}

function WalletMetricCard({
  label,
  valueMsats,
}: {
  label: string;
  valueMsats?: number;
}) {
  const hasValue = typeof valueMsats === 'number';

  return (
    <Card className='shadow-none'>
      <CardHeader className='p-3 pb-2'>
        <CardDescription className='text-[0.65rem] tracking-wide'>
          {label}
        </CardDescription>
      </CardHeader>
      <CardContent className='px-3 pb-3 pt-0'>
        <p className='text-xl font-semibold tabular-nums'>
          {hasValue ? `${formatSats(valueMsats)} sats` : '-'}
        </p>
        {hasValue && (
          <p className='text-muted-foreground text-xs tabular-nums'>
            {formatMsats(valueMsats)} msats
          </p>
        )}
      </CardContent>
    </Card>
  );
}

export function WalletBalanceStats({
  balanceMsats,
  reservedMsats,
}: WalletBalanceStatsProps) {
  return (
    <div className='grid gap-3 sm:grid-cols-2'>
      <WalletMetricCard label='Spendable' valueMsats={balanceMsats} />
      <WalletMetricCard label='Reserved' valueMsats={reservedMsats} />
    </div>
  );
}
