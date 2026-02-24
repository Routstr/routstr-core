'use client';

import { useCallback, useMemo } from 'react';
import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from 'recharts';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  ChartConfig,
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
} from '@/components/ui/chart';
import { ModelRevenueData } from '@/lib/api/services/admin';
import { convertToMsat, formatFromMsat } from '@/lib/currency';
import { useIsMobile } from '@/hooks/use-mobile';
import type { DisplayUnit } from '@/lib/types/units';

interface RevenueByModelTableProps {
  models: ModelRevenueData[];
  displayUnit: DisplayUnit;
  usdPerSat: number | null;
}

function truncateModelName(value: string, maxLength: number): string {
  if (value.length <= maxLength) {
    return value;
  }
  return `${value.slice(0, maxLength - 1)}…`;
}

export function RevenueByModelTable({
  models,
  displayUnit,
  usdPerSat,
}: RevenueByModelTableProps) {
  const isMobile = useIsMobile();

  const revenueDisplayUnit: DisplayUnit = useMemo(() => {
    if (displayUnit === 'usd' && usdPerSat === null) {
      return 'sat';
    }
    return displayUnit;
  }, [displayUnit, usdPerSat]);
  const unitLabel = revenueDisplayUnit === 'usd' ? 'USD' : revenueDisplayUnit;

  const compactNumber = useMemo(
    () =>
      new Intl.NumberFormat('en-US', {
        notation: 'compact',
        maximumFractionDigits: 1,
      }),
    []
  );

  const convertSatsToDisplay = useCallback(
    (sats: number): number => {
      if (revenueDisplayUnit === 'msat') {
        return sats * 1000;
      }
      if (revenueDisplayUnit === 'usd') {
        return sats * (usdPerSat ?? 0);
      }
      return sats;
    },
    [revenueDisplayUnit, usdPerSat]
  );

  const formatAmount = (sats: number) =>
    formatFromMsat(convertToMsat(sats, 'sat'), revenueDisplayUnit, usdPerSat);

  const formatCompactAmount = (value: number): string => {
    const compact = compactNumber.format(value);
    if (revenueDisplayUnit === 'usd') {
      return `$${compact}`;
    }
    return `${compact} ${unitLabel}`;
  };

  const totalCollectedRevenue = models.reduce(
    (sum, model) => sum + model.revenue_sats,
    0
  );
  const totalOperationalNet = models.reduce(
    (sum, model) => sum + model.net_revenue_sats,
    0
  );

  const chartData = useMemo(
    () =>
      [...models]
        .sort((a, b) => b.revenue_sats - a.revenue_sats)
        .slice(0, 12)
        .map((model) => ({
          model: model.model,
          modelLabel: truncateModelName(model.model, isMobile ? 16 : 28),
          revenueDisplay: convertSatsToDisplay(model.revenue_sats),
        })),
    [models, isMobile, convertSatsToDisplay]
  );

  const chartConfig: ChartConfig = {
    revenueDisplay: {
      label: 'Revenue',
      color: 'var(--chart-1)',
    },
  };

  if (chartData.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Revenue by Model</CardTitle>
        </CardHeader>
        <CardContent className='text-muted-foreground text-sm'>
          No model data available
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Revenue by Model</CardTitle>
        <p className='text-muted-foreground text-sm'>
          Total Collected Revenue:{' '}
          <span className='text-foreground font-mono font-medium'>
            {formatAmount(totalCollectedRevenue)}
          </span>
        </p>
        <p className='text-muted-foreground text-xs'>
          Operational Net:{' '}
          <span className='text-foreground font-mono'>
            {formatAmount(totalOperationalNet)}
          </span>
        </p>
      </CardHeader>
      <CardContent>
        <ChartContainer className='h-[360px] w-full' config={chartConfig}>
          <BarChart
            data={chartData}
            layout='vertical'
            margin={{
              top: 8,
              right: isMobile ? 8 : 28,
              left: isMobile ? 8 : 28,
              bottom: 8,
            }}
          >
            <CartesianGrid horizontal={false} className='stroke-muted/30' />
            <XAxis
              type='number'
              tickLine={false}
              axisLine={false}
              tickFormatter={(value) =>
                compactNumber.format(
                  typeof value === 'number' ? value : Number(value || 0)
                )
              }
            />
            <YAxis
              type='category'
              dataKey='modelLabel'
              tickLine={false}
              axisLine={false}
              width={isMobile ? 110 : 220}
            />
            <ChartTooltip
              cursor={false}
              content={
                <ChartTooltipContent
                  labelFormatter={(label) => String(label)}
                  formatter={(value, name) => {
                    const numericValue =
                      typeof value === 'number' ? value : Number(value || 0);
                    return (
                      <div className='grid w-full grid-cols-[minmax(0,1fr)_auto] gap-x-4'>
                        <span className='text-muted-foreground truncate pr-1'>
                          {name}
                        </span>
                        <span className='text-foreground text-right font-mono font-medium tabular-nums'>
                          {Number.isFinite(numericValue)
                            ? formatCompactAmount(numericValue)
                            : '-'}
                        </span>
                      </div>
                    );
                  }}
                />
              }
            />
            <Bar
              dataKey='revenueDisplay'
              name={`Revenue (${unitLabel})`}
              fill='var(--color-revenueDisplay)'
              radius={[0, 6, 6, 0]}
            />
          </BarChart>
        </ChartContainer>
      </CardContent>
    </Card>
  );
}
