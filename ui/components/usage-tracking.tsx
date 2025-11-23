'use client';

import { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  Activity,
  AlertCircle,
  BarChart3,
  RefreshCw,
  TrendingDown,
  TrendingUp,
} from 'lucide-react';
import {
  AdminService,
  UsageMetricDefinition,
  UsageMetricName,
} from '@/lib/api/services/admin';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  ChartConfig,
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
} from '@/components/ui/chart';
import { Area, AreaChart, CartesianGrid, XAxis } from 'recharts';
import { Skeleton } from '@/components/ui/skeleton';
import { cn } from '@/lib/utils';

const bucketOptions = [
  { label: '15 minutes', value: 15 },
  { label: '1 hour', value: 60 },
];

const rangeOptions = [
  { label: 'Last 6 hours', value: 6 },
  { label: 'Last 24 hours', value: 24 },
  { label: 'Last 72 hours', value: 72 },
];

const palette = [
  'hsl(var(--chart-1))',
  'hsl(var(--chart-2))',
  'hsl(var(--chart-3))',
  'hsl(var(--chart-4))',
  'hsl(var(--chart-5))',
];

const defaultMetrics: UsageMetricName[] = [
  'errors',
  'chat_completions_success',
];

export function UsageTracking() {
  const [bucketMinutes, setBucketMinutes] = useState(15);
  const [hours, setHours] = useState(24);
  const [selectedMetrics, setSelectedMetrics] =
    useState<UsageMetricName[]>(defaultMetrics);

  const {
    data: metricDefinitions,
    isLoading: definitionsLoading,
    isError: definitionsError,
  } = useQuery({
    queryKey: ['usage-metric-definitions'],
    queryFn: async () => AdminService.getUsageMetricDefinitions(),
    staleTime: 10 * 60 * 1000,
  });

  useEffect(() => {
    if (!metricDefinitions || !metricDefinitions.length) {
      return;
    }
    setSelectedMetrics((current) => {
      const filtered = current.filter((metric) =>
        metricDefinitions.some((definition) => definition.name === metric)
      ) as UsageMetricName[];
      if (filtered.length) {
        return filtered;
      }
      return metricDefinitions.map(
        (definition) => definition.name as UsageMetricName
      );
    });
  }, [metricDefinitions]);

  const metricsKey = useMemo(
    () => [...selectedMetrics].sort().join(','),
    [selectedMetrics]
  );

  const {
    data: metricsData,
    isLoading: metricsLoading,
    isFetching: metricsFetching,
    isError: metricsError,
    error: metricsErrorObject,
    refetch: refetchMetrics,
  } = useQuery({
    queryKey: ['usage-metrics', metricsKey, bucketMinutes, hours],
    queryFn: async () =>
      AdminService.getUsageMetrics({
        metrics: selectedMetrics,
        bucket_minutes: bucketMinutes,
        hours,
      }),
    enabled: selectedMetrics.length > 0,
    refetchInterval: 60_000,
    keepPreviousData: true,
  });

  const colorMap = useMemo(() => {
    const map: Partial<Record<UsageMetricName, string>> = {};
    metricDefinitions?.forEach((definition, index) => {
      map[definition.name] = palette[index % palette.length];
    });
    return map;
  }, [metricDefinitions]);

  const chartConfig = useMemo<ChartConfig>(() => {
    const config: ChartConfig = {};
    metricDefinitions?.forEach((definition) => {
      const color = colorMap[definition.name];
      config[definition.name] = {
        label: definition.label,
        color,
      };
    });
    return config;
  }, [metricDefinitions, colorMap]);

  const chartData = useMemo(() => {
    if (!metricsData || !metricsData.series.length) {
      return [];
    }
    const referenceSeries = metricsData.series[0];
    return referenceSeries.points.map((point, index) => {
      const base: Record<string, string | number> = {
        bucketStart: point.bucket_start,
        label: formatBucketLabel(point.bucket_start, hours),
      };
      metricsData.series.forEach((series) => {
        base[series.name] = series.points[index]?.count ?? 0;
      });
      return base;
    });
  }, [metricsData, hours]);

  const summary = useMemo(() => {
    if (!metricsData) {
      return [];
    }
    return metricsData.series.map((series) => ({
      name: series.name,
      label: series.label,
      total: series.total,
      latest: series.points.at(-1)?.count ?? 0,
      averagePerHour: series.total / Math.max(1, hours),
    }));
  }, [metricsData, hours]);

  const handleMetricToggle = (metric: UsageMetricName) => {
    setSelectedMetrics((current) => {
      if (current.includes(metric)) {
        if (current.length === 1) {
          return current;
        }
        return current.filter((item) => item !== metric);
      }
      return [...current, metric];
    });
  };

  const renderSummaryCard = (definition: UsageMetricDefinition) => {
    const stat = summary.find((item) => item.name === definition.name);
    const Icon = definition.name === 'errors' ? AlertCircle : Activity;
    return (
      <div
        key={definition.name}
        className='rounded-lg border bg-muted/20 p-4'
        style={{
          borderColor: colorMap[definition.name] ?? 'hsl(var(--border))',
        }}
      >
        <div className='flex items-center justify-between'>
          <div className='flex items-center gap-2'>
            <Icon className='h-5 w-5' />
            <span className='text-sm font-medium'>{definition.label}</span>
          </div>
          <Badge variant='outline'>
            {bucketMinutes >= 60
              ? `${bucketMinutes / 60}h buckets`
              : `${bucketMinutes}m buckets`}
          </Badge>
        </div>
        <div className='mt-3 flex items-end justify-between'>
          <div>
            <div className='text-3xl font-bold'>
              {stat ? stat.total.toLocaleString() : '—'}
            </div>
            <p className='text-muted-foreground text-xs'>
              total events in range
            </p>
          </div>
          {stat && (
            <div className='text-right text-xs'>
              <div className='flex items-center justify-end gap-1 text-muted-foreground'>
                <TrendingUp className='h-3 w-3' />
                <span>{stat.latest} latest bucket</span>
              </div>
              <div className='flex items-center justify-end gap-1 text-muted-foreground'>
                <TrendingDown className='h-3 w-3' />
                <span>{stat.averagePerHour.toFixed(2)} avg/hr</span>
              </div>
            </div>
          )}
        </div>
      </div>
    );
  };

  const isLoading =
    definitionsLoading ||
    metricsLoading ||
    (selectedMetrics.length > 0 && !metricsData);

  return (
    <Card className='shadow-sm'>
      <CardHeader className='pb-4'>
        <div className='flex flex-col gap-4 md:flex-row md:items-center md:justify-between'>
          <div>
            <CardTitle className='flex items-center gap-2 text-xl'>
              <BarChart3 className='h-5 w-5' />
              Usage Tracking
            </CardTitle>
            <CardDescription>
              Monitor errors and successful upstream traffic over time
            </CardDescription>
          </div>
          <div className='flex flex-col gap-3 sm:flex-row sm:items-center'>
            <Select
              value={hours.toString()}
              onValueChange={(value) => setHours(Number(value))}
            >
              <SelectTrigger className='w-[160px]'>
                <SelectValue placeholder='Time range' />
              </SelectTrigger>
              <SelectContent>
                {rangeOptions.map((option) => (
                  <SelectItem key={option.value} value={option.value.toString()}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select
              value={bucketMinutes.toString()}
              onValueChange={(value) => setBucketMinutes(Number(value))}
            >
              <SelectTrigger className='w-[140px]'>
                <SelectValue placeholder='Bucket size' />
              </SelectTrigger>
              <SelectContent>
                {bucketOptions.map((option) => (
                  <SelectItem key={option.value} value={option.value.toString()}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button
              variant='ghost'
              size='icon'
              onClick={() => refetchMetrics()}
              disabled={isLoading || metricsFetching}
              className='h-9 w-9'
            >
              <RefreshCw
                className={cn(
                  'h-4 w-4',
                  (metricsFetching || metricsLoading) && 'animate-spin'
                )}
              />
              <span className='sr-only'>Refresh usage metrics</span>
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className='space-y-6'>
        {definitionsError ? (
          <div className='bg-destructive/10 text-destructive flex items-center gap-2 rounded-md p-4 text-sm'>
            <AlertCircle className='h-5 w-5' />
            Failed to load metric definitions.
          </div>
        ) : (
          <div className='flex flex-wrap gap-2'>
            {metricDefinitions?.map((definition) => (
              <Button
                key={definition.name}
                variant={
                  selectedMetrics.includes(
                    definition.name as UsageMetricName
                  )
                    ? 'default'
                    : 'outline'
                }
                size='sm'
                onClick={() =>
                  handleMetricToggle(definition.name as UsageMetricName)
                }
                className='text-xs'
              >
                {definition.label}
              </Button>
            ))}
          </div>
        )}

        {metricsError && (
          <div className='bg-destructive/10 text-destructive flex items-center gap-2 rounded-md p-4 text-sm'>
            <AlertCircle className='h-5 w-5' />
            {metricsErrorObject instanceof Error
              ? metricsErrorObject.message
              : 'Failed to load usage metrics.'}
          </div>
        )}

        {isLoading ? (
          <Skeleton className='h-64 w-full' />
        ) : (
          metricsData &&
          summary.length > 0 && (
            <div className='grid gap-4 md:grid-cols-2'>
              {metricDefinitions
                ?.filter((definition) =>
                  selectedMetrics.includes(definition.name as UsageMetricName)
                )
                .map((definition) => renderSummaryCard(definition))}
            </div>
          )
        )}

        {isLoading ? (
          <Skeleton className='h-72 w-full' />
        ) : chartData.length > 0 ? (
          <ChartContainer
            config={chartConfig}
            className='h-[360px] w-full text-xs'
          >
            <AreaChart data={chartData}>
              <CartesianGrid strokeDasharray='3 3' vertical={false} />
              <XAxis dataKey='label' tickLine={false} axisLine={false} />
              <ChartTooltip
                cursor={{ strokeDasharray: '3 3' }}
                content={
                  <ChartTooltipContent
                    labelFormatter={(value) => value as string}
                  />
                }
              />
              {metricsData?.series.map((series) => (
                <Area
                  key={series.name}
                  type='monotone'
                  dataKey={series.name}
                  stroke={`var(--color-${series.name})`}
                  fill={`var(--color-${series.name})`}
                  fillOpacity={0.2}
                  strokeWidth={2}
                  dot={false}
                  isAnimationActive={false}
                />
              ))}
            </AreaChart>
          </ChartContainer>
        ) : (
          <div className='text-muted-foreground flex flex-col items-center gap-2 rounded-md border border-dashed p-10 text-sm'>
            <AlertCircle className='h-6 w-6' />
            <p>No data available for the selected window.</p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function formatBucketLabel(value: string | undefined, rangeHours: number) {
  if (!value) {
    return '';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  if (rangeHours <= 24) {
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }
  return date.toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
  });
}
