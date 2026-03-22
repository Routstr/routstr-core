'use client';

import { useEffect, useMemo, useState } from 'react';
import { format } from 'date-fns';
import { useQuery } from '@tanstack/react-query';
import { CalendarIcon, RefreshCw } from 'lucide-react';
import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from 'recharts';
import type { DateRange } from 'react-day-picker';
import { UsageMetricsChart } from '@/components/usage-metrics-chart';
import { UsageSummaryCards } from '@/components/usage-summary-cards';
import { ErrorDetailsTable } from '@/components/error-details-table';
import { TopModelsUsageChart } from '@/components/top-models-usage-chart';
import { DashboardBalanceSummary } from '@/components/dashboard-balance-summary';
import {
  AdminService,
  type UsageMetricData,
  type UsageSummary,
} from '@/lib/api/services/admin';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Calendar } from '@/components/ui/calendar';
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig as UiChartConfig,
} from '@/components/ui/chart';
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from '@/components/ui/empty';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { useCurrencyStore } from '@/lib/stores/currency';
import { fetchBtcUsdPrice, btcToSatsRate } from '@/lib/exchange-rate';
import { CheatSheet } from '@/components/landing/cheat-sheet';
import { ConfigurationService } from '@/lib/api/services/configuration';
import { AppPageShell } from '@/components/app-page-shell';
import { useIsMobile } from '@/hooks/use-mobile';
import type { DisplayUnit } from '@/lib/types/units';
import { cn } from '@/lib/utils';

type ChartDatum = Record<string, unknown> & { timestamp: string };

type ChartKeyConfig = {
  key: string;
  name: string;
  color: string;
};

type ChartConfig = {
  id: string;
  title: string;
  mobileTitle?: string;
  description: string;
  data: ChartDatum[];
  dataKeys: ChartKeyConfig[];
  totals?: Partial<Record<string, number>>;
  metricType: 'currency' | 'count';
};

const TIME_RANGE_PRESETS = [
  { value: '24h', label: 'Last 24 Hours', hours: 24 },
  { value: '7d', label: 'Last 7 Days', hours: 7 * 24 },
  { value: '30d', label: 'Last 30 Days', hours: 30 * 24 },
  { value: '3m', label: 'Last 3 Months', hours: 90 * 24 },
  { value: '12m', label: 'Last 12 Months', hours: 365 * 24 },
] as const;
const MAX_USAGE_RANGE_HOURS = 365 * 24;

type TimeRangePresetValue = (typeof TIME_RANGE_PRESETS)[number]['value'];

const DEFAULT_TIME_RANGE_PRESET =
  TIME_RANGE_PRESETS.find((option) => option.value === '7d') ??
  TIME_RANGE_PRESETS[0];

function normalizeDateRange(range: DateRange): DateRange {
  if (!range.from || !range.to) {
    return range;
  }

  if (range.from.getTime() <= range.to.getTime()) {
    return range;
  }

  return {
    from: range.to,
    to: range.from,
  };
}

function getRangeHours(range?: DateRange): number | null {
  if (!range?.from || !range.to) {
    return null;
  }

  const normalized = normalizeDateRange(range);
  const fromTime = normalized.from?.getTime();
  const toTime = normalized.to?.getTime();

  if (fromTime === undefined || toTime === undefined) {
    return null;
  }

  const diffMs = toTime - fromTime;
  const diffHours = Math.ceil(diffMs / (1000 * 60 * 60));

  return Math.max(1, diffHours);
}

function formatCompactDateRangeLabel(range?: DateRange): string {
  if (!range?.from || !range.to) {
    return 'Custom range';
  }

  const normalized = normalizeDateRange(range);
  const from = normalized.from;
  const to = normalized.to;

  if (!from || !to) {
    return 'Custom range';
  }

  const sameMonth = format(from, 'yyyy-MM') === format(to, 'yyyy-MM');
  if (sameMonth) {
    return `${format(from, 'MMM d')} - ${format(to, 'd')}`;
  }

  const sameYear = format(from, 'yyyy') === format(to, 'yyyy');
  if (sameYear) {
    return `${format(from, 'MMM d')} - ${format(to, 'MMM d')}`;
  }

  return `${format(from, 'MMM d, yyyy')} - ${format(to, 'MMM d, yyyy')}`;
}

function getAutoIntervalMinutes(hours: number): number {
  const totalMinutes = Math.max(60, Math.ceil(hours * 60));
  const targetPoints = 96;
  const idealInterval = Math.ceil(totalMinutes / targetPoints);
  const allowedIntervals = [5, 15, 30, 60, 120, 180, 240, 360, 480, 720, 1440];

  return (
    allowedIntervals.find(
      (intervalMinutes) => intervalMinutes >= idealInterval
    ) ?? allowedIntervals[allowedIntervals.length - 1]
  );
}

function SectionLoading({ label }: { label: string }) {
  if (label === 'summary') {
    return (
      <div className='grid grid-cols-1 gap-2.5 px-1 min-[380px]:grid-cols-2 sm:gap-4 sm:px-0 xl:grid-cols-4'>
        {Array.from({ length: 14 }).map((_, index) => (
          <Card key={`summary-skeleton-${index}`}>
            <CardHeader className='flex flex-row items-center justify-between space-y-0 pb-1 sm:pb-2'>
              <Skeleton
                className={cn(
                  'h-3.5',
                  index % 4 === 0 && 'w-20',
                  index % 4 === 1 && 'w-24',
                  index % 4 === 2 && 'w-28',
                  index % 4 === 3 && 'w-32'
                )}
              />
              <Skeleton className='size-6 rounded-full sm:size-8' />
            </CardHeader>
            <CardContent className='space-y-2 pt-0'>
              <Skeleton className='h-7 w-24 sm:h-8 sm:w-28' />
              <Skeleton
                className={cn(
                  'h-3',
                  index % 3 === 0 && 'w-12',
                  index % 3 === 1 && 'w-16',
                  index % 3 === 2 && 'w-20'
                )}
              />
            </CardContent>
          </Card>
        ))}
      </div>
    );
  }

  if (label === 'metrics') {
    return (
      <Card>
        <CardHeader className='space-y-3 sm:space-y-4'>
          <div className='flex gap-2 overflow-x-auto pb-1'>
            {Array.from({ length: 4 }).map((_, index) => (
              <Skeleton
                key={`metrics-tab-skeleton-${index}`}
                className={cn('h-7 rounded-md', index === 0 ? 'w-28' : 'w-20')}
              />
            ))}
          </div>
          <div className='flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between'>
            <div className='min-w-0 space-y-2'>
              <Skeleton className='h-5 w-44 max-w-full sm:h-6' />
              <Skeleton className='h-3.5 w-72 max-w-full' />
            </div>
            <Skeleton className='hidden h-8 w-8 shrink-0 rounded-md sm:block' />
          </div>
        </CardHeader>
        <CardContent className='space-y-3 sm:space-y-4'>
          <div className='flex gap-2 overflow-x-auto pb-1 sm:flex-wrap sm:overflow-visible sm:pb-0'>
            {Array.from({ length: 4 }).map((_, index) => (
              <div
                key={`metrics-chip-skeleton-${index}`}
                className='bg-muted/30 border-border/60 flex h-8 shrink-0 items-center gap-2 rounded-full border px-3'
              >
                <Skeleton className='size-2 rounded-full' />
                <Skeleton className='h-3 w-16 sm:w-20' />
                <Skeleton className='h-3 w-10 sm:w-12' />
              </div>
            ))}
          </div>
          <div className='border-border/60 bg-muted/10 relative h-[260px] w-full overflow-hidden rounded-md border p-4 sm:h-[340px]'>
            <div className='space-y-3'>
              {Array.from({ length: 3 }).map((_, index) => (
                <Skeleton
                  key={`metrics-gridline-skeleton-${index}`}
                  className='h-[1px] w-full'
                />
              ))}
            </div>
            <div className='absolute inset-x-4 bottom-4 flex items-end gap-2'>
              {Array.from({ length: 10 }).map((_, index) => (
                <Skeleton
                  key={`metrics-bar-skeleton-${index}`}
                  className={cn(
                    'w-full max-w-[26px] rounded-sm',
                    index % 5 === 0 && 'h-16',
                    index % 5 === 1 && 'h-24',
                    index % 5 === 2 && 'h-12',
                    index % 5 === 3 && 'h-28',
                    index % 5 === 4 && 'h-20'
                  )}
                />
              ))}
            </div>
          </div>
        </CardContent>
      </Card>
    );
  }

  if (label === 'revenue by model') {
    return (
      <Card>
        <CardHeader className='space-y-2'>
          <Skeleton className='h-5 w-40' />
          <Skeleton className='h-3.5 w-60 max-w-full' />
        </CardHeader>
        <CardContent className='space-y-4'>
          <div className='overflow-x-auto'>
            <div className='min-w-[680px] space-y-3 sm:min-w-[860px]'>
              <div className='grid grid-cols-9 gap-3 border-b pb-3'>
                {Array.from({ length: 9 }).map((_, index) => (
                  <Skeleton
                    key={`revenue-header-skeleton-${index}`}
                    className={cn(
                      'h-3.5',
                      index === 0 && 'w-20',
                      index > 0 && index < 5 && 'w-14 justify-self-end',
                      index === 5 && 'w-24',
                      index > 5 && 'w-16 justify-self-end'
                    )}
                  />
                ))}
              </div>
              {Array.from({ length: 5 }).map((_, rowIndex) => (
                <div
                  key={`revenue-row-skeleton-${rowIndex}`}
                  className='grid grid-cols-9 items-center gap-3 py-2'
                >
                  <Skeleton className='h-4 w-28' />
                  <Skeleton className='h-4 w-12 justify-self-end' />
                  <Skeleton className='h-4 w-12 justify-self-end' />
                  <Skeleton className='h-4 w-10 justify-self-end' />
                  <Skeleton className='h-4 w-16 justify-self-end' />
                  <div className='flex items-center gap-2'>
                    <Skeleton className='h-2.5 flex-1' />
                    <Skeleton className='h-3 w-8' />
                  </div>
                  <Skeleton className='h-4 w-14 justify-self-end' />
                  <Skeleton className='h-4 w-16 justify-self-end' />
                  <Skeleton className='h-4 w-16 justify-self-end' />
                </div>
              ))}
            </div>
          </div>
        </CardContent>
      </Card>
    );
  }

  if (label === 'errors') {
    return (
      <Card>
        <CardHeader className='space-y-2'>
          <Skeleton className='h-5 w-40' />
          <Skeleton className='h-3.5 w-60 max-w-full' />
        </CardHeader>
        <CardContent>
          <div className='max-h-[420px] max-w-full overflow-y-auto'>
            <div className='min-w-[640px] space-y-3 sm:min-w-[760px]'>
              <div className='grid grid-cols-5 gap-3 border-b pb-3'>
                <Skeleton className='h-3.5 w-20' />
                <Skeleton className='h-3.5 w-12' />
                <Skeleton className='h-3.5 w-16' />
                <Skeleton className='h-3.5 w-16' />
                <Skeleton className='h-3.5 w-16' />
              </div>
              {Array.from({ length: 6 }).map((_, rowIndex) => (
                <div
                  key={`errors-row-skeleton-${rowIndex}`}
                  className='grid grid-cols-5 items-center gap-3 py-2'
                >
                  <Skeleton className='h-4 w-28' />
                  <Skeleton className='h-5 w-16 rounded-full' />
                  <Skeleton className='h-4 w-full' />
                  <Skeleton className='h-4 w-24' />
                  <Skeleton className='h-4 w-24' />
                </div>
              ))}
            </div>
          </div>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardContent className='space-y-3 py-6'>
        <Skeleton className='h-4 w-36' />
        <Skeleton className='h-24 w-full' />
      </CardContent>
    </Card>
  );
}

function DashboardInsights({
  summary,
  isMobile,
}: {
  summary?: UsageSummary;
  isMobile: boolean;
}) {
  if (!summary) {
    return null;
  }

  const errorTypes = Object.entries(summary.error_types || {})
    .map(([type, count]) => ({
      type,
      count: typeof count === 'number' ? count : Number(count || 0),
    }))
    .filter((entry) => Number.isFinite(entry.count) && entry.count > 0)
    .sort((a, b) => b.count - a.count);

  const hasErrorTypes = errorTypes.length > 0;

  if (!hasErrorTypes) {
    return null;
  }

  const compactNumber = new Intl.NumberFormat('en-US', {
    notation: 'compact',
    maximumFractionDigits: 1,
  });
  const truncateErrorType = (value: string): string => {
    const maxLength = isMobile ? 16 : 26;
    if (value.length <= maxLength) {
      return value;
    }
    return `${value.slice(0, maxLength - 1)}…`;
  };
  const chartData = errorTypes.slice(0, 10).map(({ type, count }) => ({
    type,
    label: truncateErrorType(type),
    count,
  }));
  const totalErrors = errorTypes.reduce((sum, entry) => sum + entry.count, 0);
  const chartConfig: UiChartConfig = {
    count: {
      label: 'Errors',
      color: 'var(--chart-4)',
    },
  };

  return (
    <div className='grid gap-6'>
      {hasErrorTypes && (
        <Card>
          <CardHeader className='space-y-1'>
            <CardTitle>Error Types Distribution</CardTitle>
            <p className='text-muted-foreground text-xs sm:text-sm'>
              Top {chartData.length} categories in this range.
            </p>
          </CardHeader>
          <CardContent>
            <ChartContainer config={chartConfig} className='h-[300px] w-full'>
              <BarChart
                data={chartData}
                layout='vertical'
                margin={{
                  top: 6,
                  right: isMobile ? 8 : 20,
                  left: isMobile ? 2 : 10,
                  bottom: 6,
                }}
              >
                <CartesianGrid horizontal={false} className='stroke-muted/25' />
                <XAxis
                  type='number'
                  allowDecimals={false}
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
                  dataKey='label'
                  tickLine={false}
                  axisLine={false}
                  width={isMobile ? 120 : 200}
                />
                <ChartTooltip
                  cursor={false}
                  content={
                    <ChartTooltipContent
                      labelFormatter={(_: any, payload: readonly any[]) =>
                        String(payload?.[0]?.payload?.type ?? '')
                      }
                      formatter={(value: any, name: any) => {
                        const numericValue =
                          typeof value === 'number'
                            ? value
                            : Number(value || 0);
                        return (
                          <div className='grid w-full grid-cols-[minmax(0,1fr)_auto] gap-x-4'>
                            <span className='text-muted-foreground truncate pr-1'>
                              {name}
                            </span>
                            <span className='text-foreground text-right font-mono font-medium tabular-nums'>
                              {Number.isFinite(numericValue)
                                ? numericValue.toLocaleString()
                                : '-'}
                            </span>
                          </div>
                        );
                      }}
                    />
                  }
                />
                <Bar
                  dataKey='count'
                  name='Errors'
                  fill='var(--color-count)'
                  radius={[0, 6, 6, 0]}
                  maxBarSize={24}
                />
              </BarChart>
            </ChartContainer>
            <p className='text-muted-foreground mt-2 text-xs'>
              Total errors: {totalErrors.toLocaleString()}
            </p>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

export default function DashboardPage() {
  const [selectedPreset, setSelectedPreset] = useState<TimeRangePresetValue>(
    DEFAULT_TIME_RANGE_PRESET.value
  );
  const [customRange, setCustomRange] = useState<DateRange>();
  const [pendingCustomRange, setPendingCustomRange] = useState<DateRange>();
  const [isCustomRangeActive, setIsCustomRangeActive] = useState(false);
  const [isCustomRangePickerOpen, setIsCustomRangePickerOpen] = useState(false);
  const [isManualRefreshing, setIsManualRefreshing] = useState(false);
  const [activeChartId, setActiveChartId] = useState('revenue');
  const isMobile = useIsMobile();
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
  const activePreset =
    TIME_RANGE_PRESETS.find((option) => option.value === selectedPreset) ??
    DEFAULT_TIME_RANGE_PRESET;
  const customRangeHours = getRangeHours(customRange);
  const queryHours =
    isCustomRangeActive && customRangeHours
      ? customRangeHours
      : activePreset.hours;
  const safeQueryHours = Math.min(queryHours, MAX_USAGE_RANGE_HOURS);
  const isUsageRangeCapped = safeQueryHours < queryHours;
  const autoInterval = getAutoIntervalMinutes(safeQueryHours);
  const usageRefetchIntervalMs = useMemo(() => {
    if (safeQueryHours > 90 * 24) {
      return 4 * 60 * 60_000;
    }
    if (safeQueryHours > 30 * 24) {
      return 2 * 60 * 60_000;
    }
    if (safeQueryHours > 7 * 24) {
      return 30 * 60_000;
    }
    return 60_000;
  }, [safeQueryHours]);
  const revenueDisplayUnit: DisplayUnit = useMemo(() => {
    if (displayUnit === 'usd' && usdPerSat === null) {
      // Keep revenue charts meaningful while the USD rate is unavailable.
      return 'sat';
    }
    return displayUnit;
  }, [displayUnit, usdPerSat]);
  const revenueUnitLabel =
    revenueDisplayUnit === 'usd'
      ? 'USD'
      : revenueDisplayUnit === 'sat'
        ? 'sats'
        : revenueDisplayUnit === 'msat'
          ? 'msats'
          : revenueDisplayUnit;

  const {
    data: usageDashboardData,
    isLoading: usageDashboardLoading,
    refetch: refetchUsageDashboard,
  } = useQuery({
    queryKey: ['usage-dashboard', autoInterval, safeQueryHours],
    queryFn: () =>
      AdminService.getUsageDashboard(safeQueryHours, autoInterval, 100, 20),
    enabled: isAuthenticated,
    refetchInterval: usageRefetchIntervalMs,
    staleTime: 30_000,
  });

  const metricsData = usageDashboardData?.metrics;
  const summaryData = usageDashboardData?.summary;
  const errorData = usageDashboardData?.error_details;
  const modelUsageMixData = usageDashboardData?.model_usage_mix;
  const hasModelUsageMixMetrics =
    Array.isArray(modelUsageMixData?.metrics) &&
    modelUsageMixData.metrics.length > 0;

  const metricsLoading = usageDashboardLoading;
  const summaryLoading = usageDashboardLoading;
  const errorLoading = usageDashboardLoading;
  const metricsTotals = metricsData?.totals;

  const chartConfigs = useMemo<ChartConfig[]>(() => {
    if (!metricsData || metricsData.metrics.length === 0) {
      return [];
    }

    const metricPoints = metricsData.metrics as ChartDatum[];
    const convertRevenueMsats = (amountMsats: number): number => {
      if (revenueDisplayUnit === 'msat') {
        return amountMsats;
      }

      const sats = amountMsats / 1000;
      if (revenueDisplayUnit === 'usd') {
        return sats * (usdPerSat ?? 0);
      }

      return sats;
    };
    const revenuePoints = metricsData.metrics.map(
      (metric: UsageMetricData) => ({
        ...metric,
        revenue_display: convertRevenueMsats(metric.revenue_msats),
      })
    ) as ChartDatum[];

    return [
      {
        id: 'revenue',
        title: 'Revenue Over Time',
        mobileTitle: 'Revenue',
        description: 'Track collected revenue trends over time.',
        data: revenuePoints,
        metricType: 'currency',
        totals: metricsTotals
          ? {
              revenue_display: convertRevenueMsats(metricsTotals.revenue_msats),
            }
          : undefined,
        dataKeys: [
          {
            key: 'revenue_display',
            name: 'Revenue',
            color: 'var(--chart-1)',
          },
        ],
      },
      {
        id: 'requests',
        title: 'Request Volume',
        mobileTitle: 'Requests',
        description: 'Understand traffic and completion reliability over time.',
        data: metricPoints,
        metricType: 'count',
        totals: metricsTotals
          ? {
              total_requests: metricsTotals.total_requests,
              successful_chat_completions:
                metricsTotals.successful_chat_completions,
              failed_requests: metricsTotals.failed_requests,
            }
          : undefined,
        dataKeys: [
          {
            key: 'total_requests',
            name: 'Total Requests',
            color: 'var(--chart-1)',
          },
          {
            key: 'successful_chat_completions',
            name: 'Successful',
            color: 'var(--chart-2)',
          },
          {
            key: 'failed_requests',
            name: 'Failed',
            color: 'var(--chart-5)',
          },
        ],
      },
      {
        id: 'errors',
        title: 'Error Tracking',
        mobileTitle: 'Errors',
        description: 'Monitor warnings, handled errors, and upstream failures.',
        data: metricPoints,
        metricType: 'count',
        totals: metricsTotals
          ? {
              errors: metricsTotals.errors,
              warnings: metricsTotals.warnings,
              upstream_errors: metricsTotals.upstream_errors,
            }
          : undefined,
        dataKeys: [
          {
            key: 'errors',
            name: 'Errors',
            color: 'var(--chart-4)',
          },
          {
            key: 'warnings',
            name: 'Warnings',
            color: 'var(--chart-3)',
          },
          {
            key: 'upstream_errors',
            name: 'Upstream Errors',
            color: 'var(--chart-5)',
          },
        ],
      },
      {
        id: 'payments',
        title: 'Payment Activity',
        mobileTitle: 'Payments',
        description: 'Follow payment processing activity by interval.',
        data: metricPoints,
        metricType: 'count',
        totals: metricsTotals
          ? {
              payment_processed: metricsTotals.payment_processed,
            }
          : undefined,
        dataKeys: [
          {
            key: 'payment_processed',
            name: 'Payments Processed',
            color: 'var(--chart-2)',
          },
        ],
      },
      {
        id: 'tokens',
        title: 'Token Usage',
        mobileTitle: 'Tokens',
        description:
          'Track input, output, and total token throughput over time.',
        data: metricPoints,
        metricType: 'count',
        totals: metricsTotals
          ? {
              input_tokens: metricsTotals.input_tokens,
              output_tokens: metricsTotals.output_tokens,
              total_tokens: metricsTotals.total_tokens,
            }
          : undefined,
        dataKeys: [
          {
            key: 'total_tokens',
            name: 'Total Tokens',
            color: 'var(--chart-1)',
          },
          {
            key: 'input_tokens',
            name: 'Input Tokens',
            color: 'var(--chart-2)',
          },
          {
            key: 'output_tokens',
            name: 'Output Tokens',
            color: 'var(--chart-3)',
          },
        ],
      },
    ];
  }, [metricsData, metricsTotals, revenueDisplayUnit, usdPerSat]);

  useEffect(() => {
    if (chartConfigs.length === 0) {
      return;
    }

    if (!chartConfigs.some((config) => config.id === activeChartId)) {
      setActiveChartId(chartConfigs[0].id);
    }
  }, [chartConfigs, activeChartId]);

  if (!isAuthResolved) {
    return (
      <AppPageShell contentClassName='mx-auto w-full max-w-5xl'>
        <div className='space-y-6'>
          <div className='space-y-2'>
            <Skeleton className='h-8 w-56 max-w-full sm:h-9' />
            <Skeleton className='h-4 w-80 max-w-full' />
          </div>
          <SectionLoading label='summary' />
          <SectionLoading label='metrics' />
        </div>
      </AppPageShell>
    );
  }

  if (!isAuthenticated) {
    return <CheatSheet />;
  }

  const handleRefresh = async () => {
    if (isManualRefreshing) {
      return;
    }

    setIsManualRefreshing(true);
    try {
      await refetchUsageDashboard();
    } finally {
      setIsManualRefreshing(false);
    }
  };

  const openRangePicker = () => {
    // Force a fresh selection so the range is only applied
    // after the user explicitly chooses both start and end.
    setPendingCustomRange(undefined);
    setIsCustomRangePickerOpen(true);
  };

  const handleCustomRangePickerChange = (open: boolean) => {
    if (open) {
      openRangePicker();
      return;
    }

    setIsCustomRangePickerOpen(false);
  };

  const handleRangeSelectChange = (value: string) => {
    if (value === 'custom') {
      // Always require an explicit fresh range selection.
      openRangePicker();
      return;
    }

    const preset = TIME_RANGE_PRESETS.find((option) => option.value === value);

    if (!preset) {
      return;
    }

    setSelectedPreset(preset.value);
    setIsCustomRangeActive(false);
  };

  const handleCustomRangeSelect = (nextRange: DateRange | undefined) => {
    if (!nextRange?.from) {
      setPendingCustomRange(undefined);
      return;
    }

    const normalized = normalizeDateRange(nextRange);
    const from = normalized.from;
    const to = normalized.to;
    const hasPreviousStart = Boolean(pendingCustomRange?.from);

    if (!from) {
      setPendingCustomRange(undefined);
      return;
    }

    // DayPicker may emit from===to on the first click in range mode.
    // Keep waiting until the user explicitly picks a second (end) date.
    const isSameDay = to ? from.getTime() === to.getTime() : false;
    if (!hasPreviousStart || !to || isSameDay) {
      setPendingCustomRange({ from, to: undefined });
      return;
    }

    setPendingCustomRange(normalized);
    setCustomRange(normalized);
    setIsCustomRangeActive(true);
    setIsCustomRangePickerOpen(false);
  };

  const activeChartConfig =
    chartConfigs.find((config) => config.id === activeChartId) ??
    chartConfigs[0];
  const selectedRangeValue =
    isCustomRangeActive && customRange?.from && customRange?.to
      ? 'custom'
      : selectedPreset;
  const compactCustomRangeLabel = formatCompactDateRangeLabel(customRange);

  return (
    <AppPageShell contentClassName='mx-auto w-full max-w-5xl'>
      <div className='min-w-0 space-y-4 sm:space-y-6'>
        <section className='space-y-1 px-1'>
          <h1 className='text-lg font-semibold tracking-tight sm:text-2xl'>
            Dashboard
          </h1>
          <p className='text-muted-foreground text-sm leading-relaxed'>
            Node balances, request health, and revenue trends.
          </p>
        </section>

        <DashboardBalanceSummary
          displayUnit={displayUnit}
          usdPerSat={usdPerSat}
        />

        <section className='border-border/60 bg-card/35 space-y-4 rounded-xl border p-3 sm:p-4'>
          <div className='space-y-2'>
            <div className='flex flex-wrap items-center gap-2'>
              <h2 className='text-base leading-snug font-semibold tracking-tight sm:text-lg'>
                Usage Analytics
              </h2>
            </div>
            <p className='text-muted-foreground text-xs sm:text-sm'>
              All cards and charts in this section update from the selected
              range.
            </p>
            {isUsageRangeCapped ? (
              <p className='text-muted-foreground text-[11px] sm:text-xs'>
                Usage analytics are capped to the last{' '}
                {MAX_USAGE_RANGE_HOURS / 24} days for server safety.
              </p>
            ) : null}
          </div>

          <div className='flex flex-col gap-2 sm:flex-row sm:items-center'>
            <div className='w-full max-w-[20rem] sm:max-w-[22rem]'>
              <div className='border-input bg-card/30 dark:bg-input/30 flex h-8 w-full min-w-0 items-stretch overflow-hidden rounded-lg border sm:h-9'>
                <Popover
                  open={isCustomRangePickerOpen}
                  onOpenChange={handleCustomRangePickerChange}
                >
                  <PopoverTrigger asChild>
                    <Button
                      type='button'
                      variant='ghost'
                      size='icon'
                      id='dashboard-date-range'
                      className='text-muted-foreground hover:bg-muted/50 hover:text-foreground dark:hover:bg-input/50 h-full w-8 rounded-none border-0 bg-transparent p-0 sm:w-9'
                      aria-label='Open custom date range'
                    >
                      <CalendarIcon className='h-3.5 w-3.5' />
                    </Button>
                  </PopoverTrigger>
                  <PopoverContent
                    align='start'
                    className='w-auto overflow-hidden p-0'
                  >
                    <Calendar
                      mode='range'
                      selected={pendingCustomRange}
                      onSelect={handleCustomRangeSelect}
                      defaultMonth={pendingCustomRange?.from}
                      numberOfMonths={isMobile ? 1 : 2}
                      className='p-3'
                    />
                  </PopoverContent>
                </Popover>

                <div className='bg-border/70 w-px' />

                <Select
                  value={selectedRangeValue}
                  onValueChange={handleRangeSelectChange}
                >
                  <SelectTrigger className='h-full min-h-0 w-full rounded-none border-0 !bg-transparent px-3 py-0 text-sm font-normal shadow-none ring-0 hover:!bg-transparent focus-visible:border-transparent focus-visible:ring-0 data-[state=open]:!bg-transparent dark:!bg-transparent dark:hover:!bg-transparent'>
                    <SelectValue
                      placeholder='Select range'
                      className='text-left'
                    />
                  </SelectTrigger>
                  <SelectContent align='start'>
                    {customRange?.from && customRange?.to && (
                      <SelectItem value='custom'>
                        {compactCustomRangeLabel}
                      </SelectItem>
                    )}
                    {TIME_RANGE_PRESETS.map((option) => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>

            <Button
              onClick={handleRefresh}
              variant='outline'
              disabled={isManualRefreshing}
              className='h-8 w-full px-2.5 text-xs sm:ml-auto sm:w-auto'
            >
              <RefreshCw
                className={cn(
                  'mr-1 h-3 w-3',
                  isManualRefreshing && 'animate-spin'
                )}
              />
              {isManualRefreshing ? 'Refreshing...' : 'Refresh'}
            </Button>
          </div>

          {metricsLoading ? (
            <SectionLoading label='metrics' />
          ) : activeChartConfig ? (
            <UsageMetricsChart
              data={activeChartConfig.data}
              title={activeChartConfig.title}
              description={activeChartConfig.description}
              dataKeys={activeChartConfig.dataKeys}
              totals={activeChartConfig.totals}
              metricType={activeChartConfig.metricType}
              currencyUnitLabel={revenueUnitLabel}
              tabs={chartConfigs.map((config) => ({
                id: config.id,
                label: isMobile
                  ? (config.mobileTitle ?? config.title)
                  : config.title,
              }))}
              activeTabId={activeChartId}
              onTabChange={setActiveChartId}
            />
          ) : (
            <Card>
              <CardContent>
                <Empty className='border-none py-8'>
                  <EmptyHeader>
                    <EmptyMedia variant='icon'>
                      <RefreshCw className='h-4 w-4' />
                    </EmptyMedia>
                    <EmptyTitle>No data available</EmptyTitle>
                    <EmptyDescription>
                      No metrics data exists for this range yet. Try a broader
                      range.
                    </EmptyDescription>
                  </EmptyHeader>
                </Empty>
              </CardContent>
            </Card>
          )}

          {!metricsLoading && modelUsageMixData && hasModelUsageMixMetrics ? (
            <TopModelsUsageChart
              mix={modelUsageMixData}
              displayUnit={displayUnit}
              usdPerSat={usdPerSat}
            />
          ) : null}

          {summaryLoading ? (
            <SectionLoading label='summary' />
          ) : summaryData ? (
            <UsageSummaryCards summary={summaryData} />
          ) : null}

          <DashboardInsights summary={summaryData} isMobile={isMobile} />

          {errorLoading ? (
            <SectionLoading label='errors' />
          ) : errorData ? (
            <ErrorDetailsTable errors={errorData.errors} />
          ) : null}
        </section>
      </div>
    </AppPageShell>
  );
}
