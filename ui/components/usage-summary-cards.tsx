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

interface UsageSummaryCardsProps {
  summary: UsageSummary;
}

export function UsageSummaryCards({ summary }: UsageSummaryCardsProps) {
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
      title: 'Revenue (sats)',
      value: summary.revenue_sats.toLocaleString(undefined, {
        maximumFractionDigits: 2,
      }),
      icon: Coins,
      color: 'text-green-600',
    },
    {
      title: 'Net Revenue (sats)',
      value: summary.net_revenue_sats.toLocaleString(undefined, {
        maximumFractionDigits: 2,
      }),
      icon: DollarSign,
      color: 'text-emerald-600',
    },
    {
      title: 'Refunds (sats)',
      value: summary.refunds_sats.toLocaleString(undefined, {
        maximumFractionDigits: 2,
      }),
      icon: TrendingDown,
      color: 'text-red-500',
    },
    {
      title: 'Avg Revenue/Request',
      value: `${(summary.avg_revenue_per_request_msats / 1000).toLocaleString(undefined, {
        maximumFractionDigits: 3,
      })} sats`,
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
    <div className='grid gap-4 md:grid-cols-2 lg:grid-cols-4'>
      {cards.map((card) => (
        <Card key={card.title}>
          <CardHeader className='flex flex-row items-center justify-between space-y-0 pb-2'>
            <CardTitle className='text-sm font-medium'>{card.title}</CardTitle>
            <card.icon className={`h-4 w-4 ${card.color}`} />
          </CardHeader>
          <CardContent>
            <div className='text-2xl font-bold'>{card.value}</div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
