'use client';

import { useQuery } from '@tanstack/react-query';
import { WalletService, BalanceDetail } from '@/lib/api/services/wallet';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { convertToMsat, formatFromMsat } from '@/lib/currency';
import { Wallet, User, Coins } from 'lucide-react';
import type { DisplayUnit } from '@/lib/types/units';

interface DashboardBalanceSummaryProps {
  displayUnit?: DisplayUnit;
  usdPerSat?: number | null;
}

export function DashboardBalanceSummary({
  displayUnit = 'sat',
  usdPerSat = null,
}: DashboardBalanceSummaryProps) {
  const { data } = useQuery({
    queryKey: ['detailed-wallet-balance'],
    queryFn: async () => {
      return WalletService.getDetailedBalances();
    },
    refetchInterval: 30000,
  });

  const calculateTotals = (balances: BalanceDetail[]) => {
    let totalWallet = 0;
    let totalUser = 0;
    let totalOwner = 0;

    balances.forEach((detail) => {
      if (!detail.error) {
        const walletMsat = convertToMsat(
          detail.wallet_balance || 0,
          detail.unit
        );
        const userMsat = convertToMsat(detail.user_balance || 0, detail.unit);
        const ownerMsat = convertToMsat(detail.owner_balance || 0, detail.unit);

        totalWallet += walletMsat;
        totalUser += userMsat;
        totalOwner += ownerMsat;
      }
    });

    return { totalWallet, totalUser, totalOwner };
  };

  const totals = data
    ? calculateTotals(data)
    : { totalWallet: 0, totalUser: 0, totalOwner: 0 };

  const formatAmount = (msatAmount: number): string =>
    formatFromMsat(msatAmount, displayUnit, usdPerSat);

  const cards = [
    {
      title: 'Your Balance',
      value: formatAmount(totals.totalOwner),
      icon: Coins,
      color: 'text-green-600',
      bgColor: 'bg-green-100 dark:bg-green-900/20',
    },
    {
      title: 'Total Wallet',
      value: formatAmount(totals.totalWallet),
      icon: Wallet,
      color: 'text-blue-600',
      bgColor: 'bg-blue-100 dark:bg-blue-900/20',
    },
    {
      title: 'User Balance',
      value: formatAmount(totals.totalUser),
      icon: User,
      color: 'text-purple-600',
      bgColor: 'bg-purple-100 dark:bg-purple-900/20',
    },
  ];

  return (
    <div className='grid gap-4 md:grid-cols-3'>
      {cards.map((card) => (
        <Card key={card.title}>
          <CardHeader className='flex flex-row items-center justify-between space-y-0 pb-2'>
            <CardTitle className='text-sm font-medium'>{card.title}</CardTitle>
            <div className={`rounded-full p-2 ${card.bgColor}`}>
              <card.icon className={`h-4 w-4 ${card.color}`} />
            </div>
          </CardHeader>
          <CardContent>
            <div className='text-2xl font-bold'>{card.value}</div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

