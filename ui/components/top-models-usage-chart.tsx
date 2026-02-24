'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { ExpandIcon, Minimize2Icon } from 'lucide-react';
import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from 'recharts';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import {
  ChartConfig,
  ChartContainer,
  ChartTooltip,
} from '@/components/ui/chart';
import { useIsMobile } from '@/hooks/use-mobile';
import { type ModelUsageMix } from '@/lib/api/services/admin';
import type { DisplayUnit } from '@/lib/types/units';
import { cn } from '@/lib/utils';

interface TopModelsUsageChartProps {
  mix: ModelUsageMix;
  displayUnit: DisplayUnit;
  usdPerSat: number | null;
}

type ChartMode = 'requests' | 'revenue' | 'tokens';

interface TooltipRow {
  color: string;
  dataKey: string;
  label: string;
  value: number;
}

type LeaderboardTrend = 'up' | 'down' | 'flat' | 'new';

interface LeaderboardRow {
  chartDataKey: string | null;
  displayName: string;
  model: string;
  provider: string;
  rank: number;
  totalRaw: number;
  trend: LeaderboardTrend;
  trendPercent: number | null;
}

function parseBucketDate(value: string): Date | null {
  const normalized = value.includes('T')
    ? value
    : `${value.replace(' ', 'T')}Z`;
  const parsed = new Date(normalized);
  if (!Number.isNaN(parsed.getTime())) {
    return parsed;
  }
  const fallback = new Date(value);
  return Number.isNaN(fallback.getTime()) ? null : fallback;
}

function hueFromString(input: string): number {
  let hash = 0;
  for (let i = 0; i < input.length; i += 1) {
    hash = (hash << 5) - hash + input.charCodeAt(i);
    hash |= 0;
  }
  return Math.abs(hash) % 360;
}

function getSeriesColor(model: string, index: number): string {
  const palette = [
    'var(--chart-1)',
    'var(--chart-2)',
    'var(--chart-3)',
    'var(--chart-4)',
    'var(--chart-5)',
    '#f59e0b',
    '#06b6d4',
    '#8b5cf6',
    '#f97316',
    '#34d399',
  ];

  if (index < palette.length) {
    return palette[index];
  }

  const hue = (hueFromString(model) + index * 23) % 360;
  return `hsl(${hue} 70% 56%)`;
}

function formatTooltipTimestamp(label: string): string {
  const date = parseBucketDate(label);
  if (!date) {
    return label;
  }
  return date.toLocaleString([], {
    month: 'long',
    day: 'numeric',
    year: 'numeric',
  });
}

function formatAxisTimestamp(
  timestamp: string,
  hasMultipleDays: boolean
): string {
  const date = parseBucketDate(timestamp);
  if (!date) {
    return '';
  }

  if (hasMultipleDays) {
    return date.toLocaleDateString([], {
      month: 'short',
      day: 'numeric',
    });
  }

  return date.toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
  });
}

function convertRevenueMsats(
  amountMsats: number,
  displayUnit: DisplayUnit,
  usdPerSat: number | null
): number {
  if (displayUnit === 'msat') {
    return amountMsats;
  }

  const sats = amountMsats / 1000;
  if (displayUnit === 'usd') {
    return sats * (usdPerSat ?? 0);
  }

  return sats;
}

function prettifyProvider(provider: string): string {
  const normalized = provider.trim().toLowerCase();
  const aliasMap: Record<string, string> = {
    'x ai': 'x-ai',
    xai: 'x-ai',
    'z ai': 'z-ai',
    zai: 'z-ai',
    open_ai: 'openai',
    openai: 'openai',
  };
  if (aliasMap[normalized]) {
    return aliasMap[normalized];
  }
  return normalized.replace(/[_-]+/g, ' ');
}

function detectProviderFromModel(model: string): string {
  const value = model.toLowerCase();
  if (value.includes('claude')) return 'anthropic';
  if (value.includes('gpt') || value.includes('openai')) return 'openai';
  if (value.includes('gemini')) return 'google';
  if (value.includes('grok') || value.includes('x-ai') || value.includes('xai')) {
    return 'x-ai';
  }
  if (value.includes('deepseek')) return 'deepseek';
  if (value.includes('minimax')) return 'minimax';
  if (value.includes('kimi') || value.includes('moonshot')) return 'moonshot';
  if (value.includes('mistral')) return 'mistral';
  if (value.includes('qwen') || value.includes('alibaba')) return 'alibaba';
  if (value.includes('glm') || value.includes('z-ai') || value.includes('z ai')) {
    return 'z-ai';
  }
  return 'unknown';
}

function getModelPresentation(
  model: string
): { displayName: string; provider: string } {
  const trimmed = model.trim();
  const slashIndex = trimmed.indexOf('/');
  if (slashIndex > 0 && slashIndex < trimmed.length - 1) {
    const provider = prettifyProvider(trimmed.slice(0, slashIndex));
    const displayName = trimmed.slice(slashIndex + 1);
    return { displayName, provider };
  }

  return {
    displayName: trimmed,
    provider: detectProviderFromModel(trimmed),
  };
}

export function TopModelsUsageChart({
  mix,
  displayUnit,
  usdPerSat,
}: TopModelsUsageChartProps) {
  const [mode, setMode] = useState<ChartMode>('requests');
  const [hoveredSeriesKey, setHoveredSeriesKey] = useState<string | null>(null);
  const [isChartPointerInside, setIsChartPointerInside] = useState(false);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const isMobile = useIsMobile();
  const containerRef = useRef<HTMLDivElement>(null);
  const compactNumber = useMemo(
    () =>
      new Intl.NumberFormat('en-US', {
        notation: 'compact',
        maximumFractionDigits: 2,
      }),
    []
  );
  const mixTopModels = useMemo(
    () => (Array.isArray(mix.top_models) ? mix.top_models : []),
    [mix.top_models]
  );
  const mixMetrics = useMemo(
    () => (Array.isArray(mix.metrics) ? mix.metrics : []),
    [mix.metrics]
  );

  const chartModels = useMemo(
    () => mixTopModels.slice(0, 10),
    [mixTopModels]
  );
  const leaderboardModels = useMemo(
    () => mixTopModels.slice(0, 10),
    [mixTopModels]
  );
  const revenueDisplayUnit: DisplayUnit = useMemo(() => {
    if (displayUnit === 'usd' && usdPerSat === null) {
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

  const series = useMemo(
    () =>
      chartModels.map((model, index) => ({
        requestsKey: `model_req_${index}`,
        revenueKey: `model_rev_${index}`,
        tokensKey: `model_tok_${index}`,
        label: model,
        color: getSeriesColor(model, index),
      })),
    [chartModels]
  );

  const chartData = useMemo(
    () =>
      mixMetrics.map((metric) => {
        const modelCounts = metric.model_counts ?? {};
        const modelRevenue = metric.model_revenue_msats ?? {};
        const modelTokens = metric.model_tokens ?? {};
        const point: Record<string, number | string> = {
          timestamp: metric.timestamp,
          total_successful: metric.total_successful,
          total_revenue_msats: metric.total_revenue_msats,
          total_tokens: metric.total_tokens,
          others_requests: metric.others,
          others_revenue_msats: metric.others_revenue_msats,
          others_tokens: metric.others_tokens,
        };

        for (const item of series) {
          point[item.requestsKey] = modelCounts[item.label] ?? 0;
          point[item.revenueKey] = modelRevenue[item.label] ?? 0;
          point[item.tokensKey] = modelTokens[item.label] ?? 0;
        }

        return point;
      }),
    [mixMetrics, series]
  );

  const hasMultipleDays = useMemo(() => {
    const daySet = new Set(
      chartData.map((item) =>
        parseBucketDate(String(item.timestamp))?.toDateString()
      )
    );
    return daySet.size > 1;
  }, [chartData]);

  const chartConfig = useMemo(() => {
    const config: ChartConfig = {};
    for (const item of series) {
      config[item.requestsKey] = {
        label: item.label,
        color: item.color,
      };
      config[item.revenueKey] = {
        label: item.label,
        color: item.color,
      };
      config[item.tokensKey] = {
        label: item.label,
        color: item.color,
      };
    }
    config.others_requests = {
      label: 'Others',
      color: '#6b7280',
    };
    config.others_revenue_msats = {
      label: 'Others',
      color: '#6b7280',
    };
    config.others_tokens = {
      label: 'Others',
      color: '#6b7280',
    };
    return config;
  }, [series]);

  useEffect(() => {
    setHoveredSeriesKey(null);
    setIsChartPointerInside(false);
  }, [mode]);

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
      console.error('Failed to toggle top models chart fullscreen', error);
    }
  };

  const formatValue = (rawValue: number): string => {
    if (mode === 'requests') {
      return compactNumber.format(rawValue);
    }

    if (mode === 'tokens') {
      return compactNumber.format(rawValue);
    }

    const converted = convertRevenueMsats(
      rawValue,
      revenueDisplayUnit,
      usdPerSat
    );
    const compact = compactNumber.format(converted);
    if (revenueDisplayUnit === 'usd') {
      return `$${compact}`;
    }
    return `${compact} ${revenueUnitLabel}`;
  };

  const activeSeries = series.map((item) => ({
    dataKey:
      mode === 'requests'
        ? item.requestsKey
        : mode === 'revenue'
          ? item.revenueKey
          : item.tokensKey,
    name: item.label,
    color: item.color,
  }));
  const othersKey = (
    mode === 'requests'
      ? 'others_requests'
      : mode === 'revenue'
        ? 'others_revenue_msats'
        : 'others_tokens'
  ) as 'others_requests' | 'others_revenue_msats' | 'others_tokens';
  const activeSeriesKeys = [
    ...activeSeries.map((item) => item.dataKey),
    othersKey,
  ];
  const activeHoverSeriesKey =
    hoveredSeriesKey && activeSeriesKeys.includes(hoveredSeriesKey)
      ? hoveredSeriesKey
      : null;
  const getSeriesOpacity = (dataKey: string): number =>
    activeHoverSeriesKey && activeHoverSeriesKey !== dataKey ? 0.18 : 1;
  const formatLeaderboardTotal = (rawValue: number): string => {
    if (mode === 'requests') {
      return `${compactNumber.format(rawValue)} requests`;
    }

    if (mode === 'tokens') {
      return `${compactNumber.format(rawValue)} tokens`;
    }

    const converted = convertRevenueMsats(
      rawValue,
      revenueDisplayUnit,
      usdPerSat
    );
    const compact = compactNumber.format(converted);
    if (revenueDisplayUnit === 'usd') {
      return `$${compact}`;
    }
    return `${compact} ${revenueUnitLabel}`;
  };
  const formatTrendPercent = (value: number): string => {
    const abs = Math.abs(value);
    const rounded = abs >= 10 ? abs.toFixed(0) : abs.toFixed(1);
    return rounded.replace(/\.0$/, '');
  };
  const leaderboardRows = useMemo<LeaderboardRow[]>(() => {
    if (leaderboardModels.length === 0 || mixMetrics.length === 0) {
      return [];
    }

    const windowSize = Math.floor(mixMetrics.length / 2);
    const previousMetrics =
      windowSize > 0
        ? mixMetrics.slice(-windowSize * 2, -windowSize)
        : [];
    const currentMetrics =
      windowSize > 0 ? mixMetrics.slice(-windowSize) : mixMetrics;

    const rows = leaderboardModels
      .map((model) => {
        const readMetric = (metric: (typeof mixMetrics)[number]): number =>
          mode === 'requests'
            ? (metric.model_counts ?? {})[model] ?? 0
            : mode === 'revenue'
              ? (metric.model_revenue_msats ?? {})[model] ?? 0
              : (metric.model_tokens ?? {})[model] ?? 0;

        const totalRaw = mixMetrics.reduce(
          (sum, metric) => sum + readMetric(metric),
          0
        );
        const previousRaw = previousMetrics.reduce(
          (sum, metric) => sum + readMetric(metric),
          0
        );
        const currentRaw = currentMetrics.reduce(
          (sum, metric) => sum + readMetric(metric),
          0
        );
        const trendPercent =
          previousRaw > 0
            ? ((currentRaw - previousRaw) / previousRaw) * 100
            : null;

        let trend: LeaderboardTrend = 'flat';
        if (previousRaw <= 0 && currentRaw > 0) {
          trend = 'new';
        } else if (trendPercent !== null && trendPercent > 0.5) {
          trend = 'up';
        } else if (trendPercent !== null && trendPercent < -0.5) {
          trend = 'down';
        }

        const presentation = getModelPresentation(model);
        const matchingSeries = series.find((item) => item.label === model);
        const chartDataKey = matchingSeries
          ? mode === 'requests'
            ? matchingSeries.requestsKey
            : mode === 'revenue'
              ? matchingSeries.revenueKey
              : matchingSeries.tokensKey
          : null;

        return {
          chartDataKey,
          displayName: presentation.displayName,
          model,
          provider: presentation.provider,
          rank: 0,
          totalRaw,
          trend,
          trendPercent,
        } satisfies LeaderboardRow;
      })
      .filter((row) => row.totalRaw > 0)
      .sort((a, b) => b.totalRaw - a.totalRaw)
      .slice(0, 10)
      .map((row, index) => ({
        ...row,
        rank: index + 1,
      }));

    return rows;
  }, [leaderboardModels, mixMetrics, mode, series]);

  if (chartData.length === 0) {
    return null;
  }

  return (
    <div ref={containerRef}>
      <Card
        className={cn(isFullscreen && 'h-full rounded-none border-0 ring-0')}
      >
        <CardHeader className='space-y-3 sm:space-y-4'>
          <div className='flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between'>
            <div className='min-w-0'>
              <CardTitle className='text-base sm:text-lg'>Model Usage</CardTitle>
              <p className='text-muted-foreground mt-1 text-xs sm:text-sm'>
                Stacked requests, revenue, or tokens by model (
                {mix.interval_minutes}m buckets).
              </p>
            </div>
            <div className='flex items-center gap-2 sm:shrink-0'>
              <div className='bg-muted/25 border-border/60 flex items-center gap-1 rounded-full border p-1'>
                <Button
                  type='button'
                  size='sm'
                  variant={mode === 'requests' ? 'secondary' : 'ghost'}
                  onClick={() => setMode('requests')}
                  className='h-7 rounded-full px-2.5 text-xs'
                >
                  Requests
                </Button>
                <Button
                  type='button'
                  size='sm'
                  variant={mode === 'revenue' ? 'secondary' : 'ghost'}
                  onClick={() => setMode('revenue')}
                  className='h-7 rounded-full px-2.5 text-xs'
                >
                  Revenue
                </Button>
                <Button
                  type='button'
                  size='sm'
                  variant={mode === 'tokens' ? 'secondary' : 'ghost'}
                  onClick={() => setMode('tokens')}
                  className='h-7 rounded-full px-2.5 text-xs'
                >
                  Tokens
                </Button>
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
          </div>
        </CardHeader>
        <CardContent className='space-y-3 sm:space-y-4'>
          <ChartContainer
            config={chartConfig}
            className={cn(
              'aspect-auto w-full',
              isFullscreen
                ? 'h-[calc(100vh-220px)] min-h-[340px] sm:h-[calc(100vh-260px)] sm:min-h-[420px]'
                : 'h-[260px] sm:h-[340px]'
            )}
            onMouseLeave={() => {
              setHoveredSeriesKey(null);
              setIsChartPointerInside(false);
            }}
          >
            <BarChart
              data={chartData}
              onMouseEnter={() => setIsChartPointerInside(true)}
              onMouseMove={() => setIsChartPointerInside(true)}
              onMouseLeave={() => {
                setHoveredSeriesKey(null);
                setIsChartPointerInside(false);
              }}
              margin={{
                top: 12,
                right: isMobile ? 8 : 18,
                left: isMobile ? 0 : 8,
                bottom: 0,
              }}
            >
              <CartesianGrid vertical={false} className='stroke-muted/30' />
              <XAxis
                dataKey='timestamp'
                tickLine={false}
                axisLine={false}
                minTickGap={isMobile ? 14 : 24}
                tickFormatter={(value) =>
                  formatAxisTimestamp(String(value), hasMultipleDays)
                }
              />
              <YAxis
                tickLine={false}
                axisLine={false}
                width={isMobile ? 40 : 56}
                tickFormatter={(value) =>
                  formatValue(
                    typeof value === 'number' ? value : Number(value || 0)
                  )
                }
              />
              <ChartTooltip
                cursor={false}
                content={({ active, payload, label }) => {
                  if (!isChartPointerInside || !active || !payload?.length) {
                    return null;
                  }

                  const rows = payload
                    .map((entry) => {
                      const value =
                        typeof entry.value === 'number'
                          ? entry.value
                          : Number(entry.value || 0);

                      return {
                        color: String(entry.color || '#6b7280'),
                        dataKey: String(entry.dataKey || ''),
                        label: String(entry.name || ''),
                        value,
                      } satisfies TooltipRow;
                    })
                    .filter((row) => Number.isFinite(row.value) && row.value > 0)
                    .sort((a, b) => b.value - a.value);

                  const total = rows.reduce((sum, row) => sum + row.value, 0);
                  if (rows.length === 0) {
                    return null;
                  }

                  return (
                    <div className='border-border/50 bg-background min-w-[220px] rounded-lg border px-2.5 py-2 text-xs shadow-xl'>
                      <p className='text-foreground mb-2 text-sm font-medium'>
                        {formatTooltipTimestamp(String(label || ''))}
                      </p>
                      <div className='space-y-1.5'>
                        {rows.map((row) => (
                          <div
                            key={row.label}
                            className={cn(
                              'grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3',
                              activeHoverSeriesKey &&
                                row.dataKey !== activeHoverSeriesKey &&
                                'opacity-45'
                            )}
                          >
                            <span className='text-muted-foreground flex min-w-0 items-center gap-2'>
                              <span
                                className='h-2.5 w-1.5 shrink-0 rounded-sm'
                                style={{ backgroundColor: row.color }}
                              />
                              <span className='truncate'>{row.label}</span>
                            </span>
                            <span className='text-foreground font-mono tabular-nums'>
                              {formatValue(row.value)}
                            </span>
                          </div>
                        ))}
                      </div>
                      <div className='border-border/60 mt-2 border-t pt-2'>
                        <div className='grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3'>
                          <span className='text-muted-foreground'>Total</span>
                          <span className='text-foreground font-mono font-semibold tabular-nums'>
                            {formatValue(total)}
                          </span>
                        </div>
                      </div>
                    </div>
                  );
                }}
              />
              {activeSeries.map((item) => (
                <Bar
                  key={item.dataKey}
                  dataKey={item.dataKey}
                  name={item.name}
                  stackId='models'
                  fill={item.color}
                  fillOpacity={getSeriesOpacity(item.dataKey)}
                  maxBarSize={44}
                  onMouseEnter={() => setHoveredSeriesKey(item.dataKey)}
                  onMouseLeave={() => setHoveredSeriesKey(null)}
                />
              ))}
              <Bar
                dataKey={othersKey}
                name='Others'
                stackId='models'
                fill='#6b7280'
                fillOpacity={getSeriesOpacity(othersKey)}
                maxBarSize={44}
                onMouseEnter={() => setHoveredSeriesKey(othersKey)}
                onMouseLeave={() => setHoveredSeriesKey(null)}
              />
            </BarChart>
          </ChartContainer>

          <div className='border-border/60 space-y-2 border-t pt-3 sm:pt-4'>
            <div className='flex items-center justify-between gap-3'>
              <p className='text-muted-foreground text-xs font-medium'>
                Top models
              </p>
              <p className='text-muted-foreground text-xs'>
                Change vs prior period
              </p>
            </div>

            {leaderboardRows.length > 0 ? (
              <div className='divide-border/40 divide-y'>
                {leaderboardRows.map((row) => {
                  const rowIsLinked = Boolean(row.chartDataKey);
                  const rowIsActive =
                    row.chartDataKey !== null &&
                    activeHoverSeriesKey === row.chartDataKey;
                  const rowIsDimmed =
                    Boolean(activeHoverSeriesKey) &&
                    row.chartDataKey !== null &&
                    row.chartDataKey !== activeHoverSeriesKey;

                  let trendLabel = '0%';
                  let trendClass = 'text-muted-foreground';
                  if (row.trend === 'new') {
                    trendLabel = 'new';
                    trendClass = 'text-blue-500';
                  } else if (row.trend === 'up' && row.trendPercent !== null) {
                    trendLabel = `↑${formatTrendPercent(row.trendPercent)}%`;
                    trendClass = 'text-emerald-500';
                  } else if (
                    row.trend === 'down' &&
                    row.trendPercent !== null
                  ) {
                    trendLabel = `↓${formatTrendPercent(row.trendPercent)}%`;
                    trendClass = 'text-red-500';
                  } else if (row.trendPercent !== null) {
                    trendLabel = `${formatTrendPercent(row.trendPercent)}%`;
                  }

                  return (
                    <div
                      key={row.model}
                      className={cn(
                        'grid grid-cols-[auto_minmax(0,1fr)_auto_auto] items-center gap-3 rounded-md px-2 py-2 text-xs',
                        rowIsLinked &&
                          'cursor-pointer transition hover:bg-muted/25',
                        rowIsActive && 'bg-muted/30',
                        rowIsDimmed && 'opacity-45'
                      )}
                      title={row.model}
                      onMouseEnter={() => {
                        if (row.chartDataKey) {
                          setHoveredSeriesKey(row.chartDataKey);
                        }
                      }}
                      onMouseLeave={() => {
                        if (row.chartDataKey) {
                          setHoveredSeriesKey(null);
                        }
                      }}
                    >
                      <span className='text-muted-foreground w-5 text-right font-mono tabular-nums'>
                        {row.rank}.
                      </span>
                      <div className='min-w-0'>
                        <span className='truncate font-medium'>
                          {row.displayName}
                        </span>{' '}
                        <span className='text-muted-foreground truncate'>
                          by {row.provider}
                        </span>
                      </div>
                      <span className='text-foreground font-mono tabular-nums'>
                        {formatLeaderboardTotal(row.totalRaw)}
                      </span>
                      <span className={cn('font-medium', trendClass)}>
                        {trendLabel}
                      </span>
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className='text-muted-foreground text-xs'>
                No model totals available for this range.
              </p>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
