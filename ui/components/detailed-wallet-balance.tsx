'use client';

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Loader2, RefreshCw, AlertCircle, Wallet } from 'lucide-react';
import { WalletService, BalanceDetail } from '@/lib/api/services/wallet';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { WithdrawModal } from '@/components/withdraw-modal';
import { cn } from '@/lib/utils';

export function DetailedWalletBalance({
  refreshInterval = 10000,
}: {
  refreshInterval?: number;
}) {
  const [withdrawModalOpen, setWithdrawModalOpen] = useState(false);

  const { data, isLoading, isError, error, isFetching, refetch } = useQuery({
    queryKey: ['detailed-wallet-balance'],
    queryFn: async () => {
      return WalletService.getDetailedBalances();
    },
    refetchInterval: refreshInterval,
  });

  const calculateTotals = (balances: BalanceDetail[]) => {
    let totalWallet = 0;
    let totalUser = 0;
    let totalOwner = 0;

    balances.forEach((detail) => {
      if (!detail.error) {
        totalWallet += detail.wallet_balance || 0;
        totalUser += detail.user_balance || 0;
        totalOwner += detail.owner_balance || 0;
      }
    });

    return { totalWallet, totalUser, totalOwner };
  };

  const totals = data
    ? calculateTotals(data)
    : { totalWallet: 0, totalUser: 0, totalOwner: 0 };

  return (
    <>
      <Card className='h-full w-full shadow-sm'>
        <CardHeader className='pb-4'>
          <div className='flex items-center justify-between'>
            <CardTitle className='text-xl'>Cashu Wallet Balance</CardTitle>
            <div className='flex gap-2'>
              <Button
                variant='default'
                size='sm'
                onClick={() => setWithdrawModalOpen(true)}
                disabled={isLoading || !data || data.length === 0}
              >
                <Wallet className='mr-2 h-4 w-4' />
                Withdraw
              </Button>
              <Button
                variant='ghost'
                size='icon'
                onClick={() => refetch()}
                disabled={isLoading || isFetching}
                className='h-8 w-8'
              >
                <RefreshCw
                  className={cn(
                    'h-4 w-4',
                    (isFetching || isLoading) && 'animate-spin'
                  )}
                />
                <span className='sr-only'>Refresh balance</span>
              </Button>
            </div>
          </div>
          <CardDescription>
            Detailed balance breakdown by mint and currency
          </CardDescription>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <div className='flex items-center justify-center py-8'>
              <Loader2 className='text-primary h-8 w-8 animate-spin' />
            </div>
          ) : isError ? (
            <div className='bg-destructive/10 text-destructive flex items-center space-x-2 rounded-md p-4'>
              <AlertCircle className='h-5 w-5' />
              <span>Error loading balance: {(error as Error).message}</span>
            </div>
          ) : (
            <div className='space-y-6'>
              <div className='space-y-3'>
                <div className='flex items-center justify-between'>
                  <span className='text-muted-foreground text-sm'>
                    Your Balance (Total)
                  </span>
                  <span className='text-2xl font-bold text-green-600'>
                    {totals.totalOwner.toLocaleString()} sats
                  </span>
                </div>
                <div className='flex items-center justify-between'>
                  <span className='text-muted-foreground text-sm'>
                    Total Wallet
                  </span>
                  <span className='text-lg font-semibold'>
                    {totals.totalWallet.toLocaleString()} sats
                  </span>
                </div>
                <div className='flex items-center justify-between'>
                  <span className='text-muted-foreground text-sm'>
                    User Balance
                  </span>
                  <span className='text-lg font-semibold'>
                    {totals.totalUser.toLocaleString()} sats
                  </span>
                </div>
              </div>

              <p className='text-muted-foreground text-xs'>
                Your balance = Total wallet - User balance
              </p>

              <div className='overflow-hidden rounded-lg border'>
                {/* Desktop Table Header */}
                <div className='bg-muted hidden grid-cols-4 gap-2 p-3 text-sm font-semibold md:grid'>
                  <div>Mint / Unit</div>
                  <div className='text-right'>Wallet</div>
                  <div className='text-right'>Users</div>
                  <div className='text-right'>Owner</div>
                </div>

                {data && data.length > 0 ? (
                  data
                    .filter(
                      (detail) =>
                        (detail.wallet_balance && detail.wallet_balance > 0) ||
                        detail.error
                    )
                    .map((detail, index) => (
                      <div
                        key={index}
                        className={cn(
                          'border-t p-3 text-sm',
                          detail.error && 'bg-destructive/10 text-destructive'
                        )}
                      >
                        {/* Desktop Layout */}
                        <div className='hidden grid-cols-4 gap-2 md:grid'>
                          <div className='text-xs break-all'>
                            {detail.mint_url
                              .replace('https://', '')
                              .replace('http://', '')}{' '}
                            • {detail.unit.toUpperCase()}
                          </div>
                          <div className='text-right font-mono'>
                            {detail.error
                              ? 'error'
                              : detail.wallet_balance.toLocaleString()}
                          </div>
                          <div className='text-right font-mono'>
                            {detail.error
                              ? '-'
                              : detail.user_balance.toLocaleString()}
                          </div>
                          <div
                            className={cn(
                              'text-right font-mono',
                              !detail.error &&
                                detail.owner_balance > 0 &&
                                'font-semibold text-green-600'
                            )}
                          >
                            {detail.error
                              ? '-'
                              : detail.owner_balance.toLocaleString()}
                          </div>
                        </div>

                        {/* Mobile Layout */}
                        <div className='space-y-3 md:hidden'>
                          <div className='space-y-1'>
                            <span className='text-muted-foreground text-xs font-medium'>
                              Mint / Unit
                            </span>
                            <div className='font-mono text-xs break-all'>
                              {detail.mint_url
                                .replace('https://', '')
                                .replace('http://', '')}{' '}
                              • {detail.unit.toUpperCase()}
                            </div>
                          </div>
                          <div className='grid grid-cols-3 gap-2'>
                            <div className='space-y-1'>
                              <div className='text-muted-foreground text-xs font-medium'>
                                Wallet
                              </div>
                              <div className='truncate font-mono text-sm'>
                                {detail.error
                                  ? 'error'
                                  : detail.wallet_balance.toLocaleString()}
                              </div>
                            </div>
                            <div className='space-y-1'>
                              <div className='text-muted-foreground text-xs font-medium'>
                                Users
                              </div>
                              <div className='truncate font-mono text-sm'>
                                {detail.error
                                  ? '-'
                                  : detail.user_balance.toLocaleString()}
                              </div>
                            </div>
                            <div className='space-y-1'>
                              <div className='text-muted-foreground text-xs font-medium'>
                                Owner
                              </div>
                              <div
                                className={cn(
                                  'truncate font-mono text-sm',
                                  !detail.error &&
                                    detail.owner_balance > 0 &&
                                    'font-semibold text-green-600'
                                )}
                              >
                                {detail.error
                                  ? '-'
                                  : detail.owner_balance.toLocaleString()}
                              </div>
                            </div>
                          </div>
                        </div>
                      </div>
                    ))
                ) : (
                  <div className='text-muted-foreground p-4 text-center text-sm'>
                    No balances to display
                  </div>
                )}
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      <WithdrawModal
        open={withdrawModalOpen}
        onOpenChange={setWithdrawModalOpen}
        balances={data || []}
        onSuccess={() => {
          refetch();
        }}
      />
    </>
  );
}
