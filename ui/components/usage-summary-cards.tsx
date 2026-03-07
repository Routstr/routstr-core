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
  const totalTokens = Number(summary.total_tokens ?? 0);
  const avgTotalTokensPerCompletion = Number(
    summary.avg_total_tokens_per_completion ?? 0
  );

  const cards = [
    {
      title: 'Total Requests',
      value: summary.total_requests.toLocaleString(),
      icon: Activity,
      iconClassName: 'text-blue-600 dark:text-blue-300',
    },
    {
      title: 'Successful Completions',
      value: summary.successful_chat_completions.toLocaleString(),
      icon: CheckCircle2,
      iconClassName: 'text-emerald-600 dark:text-emerald-300',
    },
    {
      title: 'Total Tokens',
      value: totalTokens.toLocaleString(),
      icon: Database,
      iconClassName: 'text-cyan-600 dark:text-cyan-300',
    },
    {
      title: 'Avg Tokens/Completion',
      value: avgTotalTokensPerCompletion.toLocaleString(undefined, {
        maximumFractionDigits: 1,
      }),
      icon: Activity,
      iconClassName: 'text-indigo-600 dark:text-indigo-300',
    },
    {
      title: 'Revenue',
      value: formatAmount(summary.revenue_msats),
      icon: Coins,
      iconClassName: 'text-amber-600 dark:text-amber-300',
    },
    {
      title: 'Operational Net',
      value: formatAmount(summary.net_revenue_msats),
      icon: DollarSign,
      iconClassName: 'text-lime-600 dark:text-lime-300',
    },
    {
      title: 'Reverted Holds',
      value: formatAmount(summary.refunds_msats),
      icon: TrendingDown,
      iconClassName: 'text-rose-600 dark:text-rose-300',
    },
    {
      title: 'Avg Revenue/Request',
      value: formatAmount(summary.avg_revenue_per_request_msats),
      icon: CreditCard,
      iconClassName: 'text-violet-600 dark:text-violet-300',
    },
    {
      title: 'Success Rate',
      value: `${summary.success_rate.toFixed(1)}%`,
      icon: TrendingUp,
      iconClassName: 'text-teal-600 dark:text-teal-300',
    },
    {
      title: 'Refund Rate',
      value: `${summary.refund_rate.toFixed(1)}%`,
      icon: XCircle,
      iconClassName: 'text-fuchsia-600 dark:text-fuchsia-300',
    },
    {
      title: 'Failed Requests',
      value: summary.failed_requests.toLocaleString(),
      icon: XCircle,
      iconClassName: 'text-red-600 dark:text-red-300',
    },
    {
      title: 'Errors',
      value: summary.total_errors.toLocaleString(),
      icon: AlertTriangle,
      iconClassName: 'text-orange-600 dark:text-orange-300',
    },
    {
      title: 'Unique Models',
      value: summary.unique_models_count.toLocaleString(),
      icon: Database,
      iconClassName: 'text-cyan-600 dark:text-cyan-300',
    },
    {
      title: 'Upstream Errors',
      value: summary.upstream_errors.toLocaleString(),
      icon: AlertTriangle,
      iconClassName: 'text-pink-600 dark:text-pink-300',
    },
  ];

  return (
    <div className='grid grid-cols-1 gap-2.5 px-1 min-[380px]:grid-cols-2 sm:gap-4 sm:px-0 xl:grid-cols-4'>
      {cards.map((card) => (
        <Card key={card.title} size='sm'>
          <CardHeader className='flex flex-row items-center justify-between space-y-0 pb-1'>
            <CardTitle className='text-muted-foreground text-[11px] font-medium sm:text-sm'>
              {card.title}
            </CardTitle>
            <span className='inline-flex size-6 items-center justify-center sm:size-7'>
              <card.icon
                className={`size-3.5 sm:size-4 ${card.iconClassName}`}
              />
            </span>
          </CardHeader>
          <CardContent className='pt-0'>
            <div className='text-base font-semibold break-words tabular-nums sm:text-2xl'>
              {card.value}
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
