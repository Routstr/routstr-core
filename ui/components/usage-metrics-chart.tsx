'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { ExpandIcon, Minimize2Icon } from 'lucide-react';
import { Area, AreaChart, XAxis, YAxis, CartesianGrid } from 'recharts';
import {
  ChartConfig,
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
} from '@/components/ui/chart';
import { cn } from '@/lib/utils';
import { useIsMobile } from '@/hooks/use-mobile';

interface UsageMetricsChartProps {
  data: Array<Record<string, unknown> & { timestamp: string }>;
  title: string;
  description?: string;
  dataKeys: Array<{
    key: string;
    name: string;
    color: string;
  }>;
  totals?: Partial<Record<string, number>>;
  metricType?: 'currency' | 'count';
  currencyUnitLabel?: string;
  tabs?: Array<{ id: string; label: string }>;
  activeTabId?: string;
  onTabChange?: (tabId: string) => void;
}

export function UsageMetricsChart({
  data,
  title,
  description,
  dataKeys,
  totals,
  metricType = 'count',
  currencyUnitLabel = 'sat',
  tabs,
  activeTabId,
  onTabChange,
}: UsageMetricsChartProps) {
  const [hiddenSeries, setHiddenSeries] = useState<Set<string>>(new Set());
  const [isFullscreen, setIsFullscreen] = useState(false);
  const isMobile = useIsMobile();
  const containerRef = useRef<HTMLDivElement>(null);

  const compactNumber = useMemo(
    () =>
      new Intl.NumberFormat('en-US', {
        notation: 'compact',
        maximumFractionDigits: 1,
      }),
    []
  );

  const hasMultipleDays = useMemo(() => {
    const daySet = new Set(
      data.map((item) => new Date(item.timestamp).toDateString())
    );
    return daySet.size > 1;
  }, [data]);

  const formatAxisTick = (timestamp: string): string => {
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) {
      return '';
    }

    if (hasMultipleDays) {
      return date.toLocaleString([], {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
      });
    }

    return date.toLocaleTimeString([], {
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  const formatMetricValue = (value: number): string => {
    const formatted = compactNumber.format(value);
    return metricType === 'currency'
      ? `${formatted} ${currencyUnitLabel}`
      : formatted;
  };

  const metricTotals = useMemo(() => {
    const fallbackTotals = dataKeys.reduce<Record<string, number>>(
      (acc, dataKey) => {
        acc[dataKey.key] = 0;
        return acc;
      },
      {}
    );

    for (const point of data) {
      for (const dataKey of dataKeys) {
        const rawValue = point?.[dataKey.key];
        const value =
          typeof rawValue === 'number' ? rawValue : Number(rawValue || 0);
        if (Number.isFinite(value)) {
          fallbackTotals[dataKey.key] += value;
        }
      }
    }

    if (!totals) {
      return fallbackTotals;
    }

    const mergedTotals = { ...fallbackTotals };
    for (const dataKey of dataKeys) {
      const rawTotal = totals[dataKey.key];
      if (typeof rawTotal === 'number' && Number.isFinite(rawTotal)) {
        mergedTotals[dataKey.key] = rawTotal;
      }
    }

    return mergedTotals;
  }, [data, dataKeys, totals]);

  const metricChips = useMemo(
    () =>
      dataKeys.map((dataKey) => ({
        ...dataKey,
        value: Number.isFinite(metricTotals[dataKey.key])
          ? metricTotals[dataKey.key]
          : 0,
      })),
    [dataKeys, metricTotals]
  );

  const visibleDataKeys = dataKeys.filter(
    (dataKey) => !hiddenSeries.has(dataKey.key)
  );

  const toggleSeries = (key: string) => {
    setHiddenSeries((current) => {
      const next = new Set(current);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  };

  const chartConfig = dataKeys.reduce<ChartConfig>((acc, keyConfig) => {
    acc[keyConfig.key] = {
      label: keyConfig.name,
      color: keyConfig.color,
    };
    return acc;
  }, {});

  useEffect(() => {
    const handleFullscreenChange = () => {
      setIsFullscreen(document.fullscreenElement === containerRef.current);
    };

    document.addEventListener('fullscreenchange', handleFullscreenChange);

    return () => {
      document.removeEventListener('fullscreenchange', handleFullscreenChange);
    };
  }, []);

  const toggleFullscreen = async () => {
    if (!containerRef.current) {
      return;
    }

    try {
      if (document.fullscreenElement === containerRef.current) {
        await document.exitFullscreen();
      } else {
        await containerRef.current.requestFullscreen();
      }
    } catch (error) {
      console.error('Failed to toggle fullscreen analytics chart', error);
    }
  };

  return (
    <div ref={containerRef}>
      <Card
        className={cn(isFullscreen && 'h-full rounded-none border-0 ring-0')}
      >
        <CardHeader className='space-y-3 sm:space-y-4'>
          {tabs && activeTabId && onTabChange ? (
            <Tabs
              value={activeTabId}
              onValueChange={onTabChange}
              className='w-full'
            >
              <TabsList
                variant='line'
                className='max-w-full overflow-x-auto border-b-0 pb-1 whitespace-nowrap'
              >
                {tabs.map((tab) => (
                  <TabsTrigger key={tab.id} value={tab.id}>
                    {tab.label}
                  </TabsTrigger>
                ))}
              </TabsList>
            </Tabs>
          ) : null}
          <div className='flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between'>
            <div className='min-w-0'>
              <CardTitle className='text-base sm:text-lg'>{title}</CardTitle>
              {description ? (
                <p className='text-muted-foreground mt-1 text-xs sm:text-sm'>
                  {description}
                </p>
              ) : null}
            </div>
            <Button
              type='button'
              variant='outline'
              size='icon'
              className='hidden h-8 w-8 shrink-0 sm:inline-flex'
              onClick={toggleFullscreen}
            >
              {isFullscreen ? (
                <Minimize2Icon className='h-4 w-4' />
              ) : (
                <ExpandIcon className='h-4 w-4' />
              )}
              <span className='sr-only'>
                {isFullscreen
                  ? 'Exit fullscreen chart'
                  : 'Enter fullscreen chart'}
              </span>
            </Button>
          </div>
        </CardHeader>
        <CardContent className='space-y-3 sm:space-y-4'>
          <div className='flex gap-2 overflow-x-auto pb-1 sm:flex-wrap sm:overflow-visible sm:pb-0'>
            {metricChips.map((metric) => {
              const hidden = hiddenSeries.has(metric.key);

              return (
                <Button
                  key={metric.key}
                  type='button'
                  size='sm'
                  variant={hidden ? 'ghost' : 'secondary'}
                  className={cn(
                    'h-8 shrink-0 rounded-full px-3 text-xs transition-colors sm:text-sm',
                    hidden
                      ? 'text-muted-foreground/55 hover:text-muted-foreground/70 hover:bg-muted/25'
                      : 'text-foreground'
                  )}
                  onClick={() => toggleSeries(metric.key)}
                >
                  <span
                    className={cn(
                      'size-2 rounded-full',
                      hidden && 'opacity-35'
                    )}
                    style={{ backgroundColor: metric.color }}
                  />
                  <span className='max-w-[9rem] truncate sm:max-w-none'>
                    {metric.name}
                  </span>
                  <span
                    className={cn(
                      'text-xs',
                      hidden
                        ? 'text-muted-foreground/45'
                        : 'text-muted-foreground'
                    )}
                  >
                    {formatMetricValue(metric.value)}
                  </span>
                </Button>
              );
            })}
          </div>

          <ChartContainer
            className={cn(
              'aspect-auto w-full',
              isFullscreen
                ? 'h-[calc(100vh-220px)] min-h-[340px] sm:h-[calc(100vh-260px)] sm:min-h-[420px]'
                : 'h-[260px] sm:h-[340px]'
            )}
            config={chartConfig}
          >
            <AreaChart
              data={data}
              margin={{ top: 8, right: isMobile ? 0 : 12, left: 0, bottom: 0 }}
            >
              <defs>
                {dataKeys.map((dataKey) => (
                  <linearGradient
                    key={dataKey.key}
                    id={`color${dataKey.key}`}
                    x1='0'
                    y1='0'
                    x2='0'
                    y2='1'
                  >
                    <stop
                      offset='5%'
                      stopColor={dataKey.color}
                      stopOpacity={0.3}
                    />
                    <stop
                      offset='95%'
                      stopColor={dataKey.color}
                      stopOpacity={0}
                    />
                  </linearGradient>
                ))}
              </defs>
              <CartesianGrid
                vertical={false}
                strokeDasharray='3 3'
                className='stroke-muted/30'
              />
              <XAxis
                dataKey='timestamp'
                className='text-xs'
                tick={{ fill: 'var(--muted-foreground)' }}
                tickFormatter={formatAxisTick}
                tickLine={false}
                axisLine={false}
                minTickGap={isMobile ? 20 : 32}
              />
              <YAxis
                className='text-xs'
                tick={{ fill: 'var(--muted-foreground)' }}
                tickFormatter={(value) =>
                  compactNumber.format(
                    typeof value === 'number' ? value : Number(value || 0)
                  )
                }
                tickLine={false}
                axisLine={false}
                width={isMobile ? 40 : 48}
              />
              <ChartTooltip
                cursor={false}
                content={
                  <ChartTooltipContent
                    labelFormatter={(label) =>
                      new Date(String(label)).toLocaleString()
                    }
                  />
                }
              />
              {visibleDataKeys.map((dataKey) => (
                <Area
                  key={dataKey.key}
                  type='monotone'
                  dataKey={dataKey.key}
                  stroke={dataKey.color}
                  fillOpacity={1}
                  fill={`url(#color${dataKey.key})`}
                  name={dataKey.name}
                  strokeWidth={2}
                  connectNulls
                  animationDuration={1000}
                />
              ))}
            </AreaChart>
          </ChartContainer>
          {visibleDataKeys.length === 0 ? (
            <p className='text-muted-foreground text-sm'>
              Select at least one metric to display the chart.
            </p>
          ) : null}
        </CardContent>
      </Card>
    </div>
  );
}
