'use client';

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { AppSidebar } from '@/components/app-sidebar';
import { SiteHeader } from '@/components/site-header';
import { SidebarInset, SidebarProvider } from '@/components/ui/sidebar';
import { UsageMetricsChart } from '@/components/usage-metrics-chart';
import { UsageSummaryCards } from '@/components/usage-summary-cards';
import { ErrorDetailsTable } from '@/components/error-details-table';
import { RevenueByModelTable } from '@/components/revenue-by-model-table';
import { AdminService } from '@/lib/api/services/admin';
import { Button } from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { RefreshCw } from 'lucide-react';

export default function UsagePage() {
  const [timeRange, setTimeRange] = useState('24');
  const [interval, setInterval] = useState('15');

  const {
    data: metricsData,
    isLoading: metricsLoading,
    refetch: refetchMetrics,
  } = useQuery({
    queryKey: ['usage-metrics', interval, timeRange],
    queryFn: () =>
      AdminService.getUsageMetrics(parseInt(interval), parseInt(timeRange)),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const {
    data: summaryData,
    isLoading: summaryLoading,
    refetch: refetchSummary,
  } = useQuery({
    queryKey: ['usage-summary', timeRange],
    queryFn: () => AdminService.getUsageSummary(parseInt(timeRange)),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const {
    data: errorData,
    isLoading: errorLoading,
    refetch: refetchErrors,
  } = useQuery({
    queryKey: ['usage-errors', timeRange],
    queryFn: () => AdminService.getErrorDetails(parseInt(timeRange), 100),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const {
    data: revenueByModelData,
    isLoading: revenueByModelLoading,
    refetch: refetchRevenueByModel,
  } = useQuery({
    queryKey: ['revenue-by-model', timeRange],
    queryFn: () => AdminService.getRevenueByModel(parseInt(timeRange), 20),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const handleRefresh = () => {
    refetchMetrics();
    refetchSummary();
    refetchErrors();
    refetchRevenueByModel();
  };

  return (
    <SidebarProvider>
      <AppSidebar variant='inset' />
      <SidebarInset className='p-0'>
        <SiteHeader />
        <div className='container max-w-7xl px-4 py-8 md:px-6 lg:px-8'>
          <div className='mb-8 flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between'>
            <div>
              <h1 className='text-3xl font-bold tracking-tight'>
                Usage Tracking
              </h1>
              <p className='text-muted-foreground mt-2'>
                Monitor system usage, requests, and errors over time
              </p>
            </div>
            <div className='flex items-center gap-4'>
              <Select value={timeRange} onValueChange={setTimeRange}>
                <SelectTrigger className='w-[180px]'>
                  <SelectValue placeholder='Select time range' />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value='1'>Last Hour</SelectItem>
                  <SelectItem value='6'>Last 6 Hours</SelectItem>
                  <SelectItem value='24'>Last 24 Hours</SelectItem>
                  <SelectItem value='72'>Last 3 Days</SelectItem>
                  <SelectItem value='168'>Last Week</SelectItem>
                </SelectContent>
              </Select>
              <Select value={interval} onValueChange={setInterval}>
                <SelectTrigger className='w-[180px]'>
                  <SelectValue placeholder='Select interval' />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value='5'>5 Minutes</SelectItem>
                  <SelectItem value='15'>15 Minutes</SelectItem>
                  <SelectItem value='30'>30 Minutes</SelectItem>
                  <SelectItem value='60'>1 Hour</SelectItem>
                </SelectContent>
              </Select>
              <Button onClick={handleRefresh} variant='outline' size='icon'>
                <RefreshCw className='h-4 w-4' />
              </Button>
            </div>
          </div>

          <div className='space-y-6'>
            {summaryLoading ? (
              <div className='text-center py-8'>Loading summary...</div>
            ) : summaryData ? (
              <UsageSummaryCards summary={summaryData} />
            ) : null}

            <div className='grid gap-6 lg:grid-cols-2'>
              {metricsLoading ? (
                <div className='text-center py-8 col-span-2'>
                  Loading metrics...
                </div>
              ) : metricsData && metricsData.metrics.length > 0 ? (
                <>
                  <UsageMetricsChart
                    data={metricsData.metrics as Array<Record<string, unknown> & { timestamp: string }>}
                    title='Request Volume'
                    dataKeys={[
                      {
                        key: 'total_requests',
                        name: 'Total Requests',
                        color: '#3b82f6',
                      },
                      {
                        key: 'successful_chat_completions',
                        name: 'Successful',
                        color: '#10b981',
                      },
                      {
                        key: 'failed_requests',
                        name: 'Failed',
                        color: '#ef4444',
                      },
                    ]}
                  />
                  <UsageMetricsChart
                    data={metricsData.metrics.map((m) => ({
                      ...m,
                      revenue_sats: m.revenue_msats / 1000,
                      refunds_sats: m.refunds_msats / 1000,
                      net_revenue_sats:
                        (m.revenue_msats - m.refunds_msats) / 1000,
                    })) as Array<Record<string, unknown> & { timestamp: string }>}
                    title='Revenue Over Time (sats)'
                    dataKeys={[
                      {
                        key: 'revenue_sats',
                        name: 'Revenue',
                        color: '#10b981',
                      },
                      {
                        key: 'refunds_sats',
                        name: 'Refunds',
                        color: '#ef4444',
                      },
                      {
                        key: 'net_revenue_sats',
                        name: 'Net Revenue',
                        color: '#059669',
                      },
                    ]}
                  />
                  <UsageMetricsChart
                    data={metricsData.metrics as Array<Record<string, unknown> & { timestamp: string }>}
                    title='Error Tracking'
                    dataKeys={[
                      {
                        key: 'errors',
                        name: 'Errors',
                        color: '#f97316',
                      },
                      {
                        key: 'warnings',
                        name: 'Warnings',
                        color: '#eab308',
                      },
                      {
                        key: 'upstream_errors',
                        name: 'Upstream Errors',
                        color: '#ef4444',
                      },
                    ]}
                  />
                  <UsageMetricsChart
                    data={metricsData.metrics as Array<Record<string, unknown> & { timestamp: string }>}
                    title='Payment Activity'
                    dataKeys={[
                      {
                        key: 'payment_processed',
                        name: 'Payments Processed',
                        color: '#6366f1',
                      },
                    ]}
                  />
                </>
              ) : (
                <Card className='col-span-2'>
                  <CardHeader>
                    <CardTitle>No Data Available</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <p className='text-muted-foreground'>
                      No metrics data found for the selected time range. This
                      could be because no requests have been logged yet or the
                      log files are not available.
                    </p>
                  </CardContent>
                </Card>
              )}
            </div>

            {revenueByModelLoading ? (
              <div className='text-center py-8'>Loading revenue by model...</div>
            ) : revenueByModelData && revenueByModelData.models.length > 0 ? (
              <RevenueByModelTable 
                models={revenueByModelData.models}
                totalRevenue={revenueByModelData.total_revenue_sats}
              />
            ) : null}

            {errorLoading ? (
              <div className='text-center py-8'>Loading errors...</div>
            ) : errorData ? (
              <ErrorDetailsTable errors={errorData.errors} />
            ) : null}

            {summaryData && summaryData.unique_models.length > 0 && (
              <Card>
                <CardHeader>
                  <CardTitle>Active Models</CardTitle>
                </CardHeader>
                <CardContent>
                  <div className='flex flex-wrap gap-2'>
                    {summaryData.unique_models.map((model) => (
                      <span
                        key={model}
                        className='bg-secondary text-secondary-foreground inline-flex items-center rounded-md px-2.5 py-0.5 text-xs font-semibold'
                      >
                        {model}
                      </span>
                    ))}
                  </div>
                </CardContent>
              </Card>
            )}

            {summaryData &&
              summaryData.error_types &&
              Object.keys(summaryData.error_types).length > 0 && (
                <Card>
                  <CardHeader>
                    <CardTitle>Error Types Distribution</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <div className='space-y-2'>
                      {Object.entries(summaryData.error_types)
                        .sort(([, a], [, b]) => b - a)
                        .map(([type, count]) => (
                          <div
                            key={type}
                            className='flex items-center justify-between'
                          >
                            <span className='text-sm font-medium'>{type}</span>
                            <span className='text-muted-foreground text-sm'>
                              {count}
                            </span>
                          </div>
                        ))}
                    </div>
                  </CardContent>
                </Card>
              )}
          </div>
        </div>
      </SidebarInset>
    </SidebarProvider>
  );
}
