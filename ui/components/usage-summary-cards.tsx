'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { UsageSummary } from '@/lib/api/services/admin';
import {
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Activity,
  Database,
  CreditCard,
  TrendingUp,
  DollarSign,
  TrendingDown,
  Coins,
} from 'lucide-react';
import { useCurrencyStore } from '@/lib/stores/currency';
import { useQuery } from '@tanstack/react-query';
import { fetchBtcUsdPrice, btcToSatsRate } from '@/lib/exchange-rate';
import { formatFromMsat } from '@/lib/currency';

interface UsageSummaryCardsProps {
  summary: UsageSummary;
}

export function UsageSummaryCards({ summary }: UsageSummaryCardsProps) {
  const { displayUnit } = useCurrencyStore();
  const { data: btcUsdPrice } = useQuery({
    queryKey: ['btc-usd-price'],
    queryFn: fetchBtcUsdPrice,
    refetchInterval: 120_000,
    staleTime: 60_000,
  });
  const usdPerSat = btcUsdPrice ? btcToSatsRate(btcUsdPrice) : null;

  const formatAmount = (msat: number) =>
    formatFromMsat(msat, displayUnit, usdPerSat);

  const cards = [
    {
      title: 'Total Requests',
      value: summary.total_requests.toLocaleString(),
      icon: Activity,
      color: 'text-blue-500',
    },
    {
      title: 'Successful Completions',
      value: summary.successful_chat_completions.toLocaleString(),
      icon: CheckCircle2,
      color: 'text-green-500',
    },
    {
      title: 'Revenue',
      value: formatAmount(summary.revenue_msats),
      icon: Coins,
      color: 'text-green-600',
    },
    {
      title: 'Net Revenue',
      value: formatAmount(summary.net_revenue_msats),
      icon: DollarSign,
      color: 'text-emerald-600',
    },
    {
      title: 'Refunds',
      value: formatAmount(summary.refunds_msats),
      icon: TrendingDown,
      color: 'text-red-500',
    },
    {
      title: 'Avg Revenue/Request',
      value: formatAmount(summary.avg_revenue_per_request_msats),
      icon: CreditCard,
      color: 'text-cyan-500',
    },
    {
      title: 'Success Rate',
      value: `${summary.success_rate.toFixed(1)}%`,
      icon: TrendingUp,
      color: 'text-emerald-500',
    },
    {
      title: 'Refund Rate',
      value: `${summary.refund_rate.toFixed(1)}%`,
      icon: XCircle,
      color: 'text-orange-500',
    },
    {
      title: 'Failed Requests',
      value: summary.failed_requests.toLocaleString(),
      icon: XCircle,
      color: 'text-red-400',
    },
    {
      title: 'Errors',
      value: summary.total_errors.toLocaleString(),
      icon: AlertTriangle,
      color: 'text-orange-500',
    },
    {
      title: 'Unique Models',
      value: summary.unique_models_count.toLocaleString(),
      icon: Database,
      color: 'text-purple-500',
    },
    {
      title: 'Upstream Errors',
      value: summary.upstream_errors.toLocaleString(),
      icon: AlertTriangle,
      color: 'text-yellow-500',
    },
  ];

  return (
    <div className='grid gap-6 md:grid-cols-2 lg:grid-cols-4'>
      {cards.map((card) => (
        <Card key={card.title} className='hover:bg-muted/50 transition-colors'>
          <CardHeader className='flex flex-row items-center justify-between space-y-0 pb-2'>
            <CardTitle className='text-muted-foreground text-sm font-medium'>
              {card.title}
            </CardTitle>
            <div className={`bg-secondary rounded-full p-2`}>
              <card.icon className={`h-4 w-4 ${card.color}`} />
            </div>
          </CardHeader>
          <CardContent>
            <div className='text-2xl font-bold tracking-tight'>
              {card.value}
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
