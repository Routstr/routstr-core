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
      color: 'text-green-600 dark:text-green-300',
    },
    {
      title: 'Total Wallet',
      value: formatAmount(totals.totalWallet),
      icon: Wallet,
      color: 'text-blue-600 dark:text-blue-300',
    },
    {
      title: 'User Balance',
      value: formatAmount(totals.totalUser),
      icon: User,
      color: 'text-purple-600 dark:text-purple-300',
    },
  ];

  return (
    <div className='grid grid-cols-2 gap-2.5 max-[359px]:grid-cols-1 sm:gap-3 lg:grid-cols-3'>
      {cards.map((card) => (
        <Card key={card.title} size='sm' className='min-h-[6.25rem] sm:min-h-[7rem]'>
          <CardHeader className='flex flex-row items-center justify-between space-y-0 pb-1'>
            <CardTitle className='text-muted-foreground text-[11px] font-medium sm:text-sm'>
              {card.title}
            </CardTitle>
            <div className='flex size-6 items-center justify-center sm:size-7'>
              <card.icon className={`h-3.5 w-3.5 sm:h-4 sm:w-4 ${card.color}`} />
            </div>
          </CardHeader>
          <CardContent className='pt-0'>
            <div className='break-words text-base font-semibold sm:text-xl'>
              {card.value}
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
