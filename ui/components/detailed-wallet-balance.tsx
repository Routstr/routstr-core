'use client';

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { RefreshCw, AlertCircle, Wallet, User, Coins } from 'lucide-react';
import { WalletService, BalanceDetail } from '@/lib/api/services/wallet';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Alert, AlertDescription } from '@/components/ui/alert';
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from '@/components/ui/empty';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { WithdrawModal } from '@/components/withdraw-modal';
import { cn } from '@/lib/utils';
import type { DisplayUnit } from '@/lib/types/units';
import { convertToMsat, formatFromMsat } from '@/lib/currency';

export function DetailedWalletBalance({
  refreshInterval = 10000,
  displayUnit,
  usdPerSat,
}: {
  refreshInterval?: number;
  displayUnit: DisplayUnit;
  usdPerSat: number | null;
}) {
  const [withdrawModalOpen, setWithdrawModalOpen] = useState(false);

  const { data, isLoading, isError, error, isFetching, refetch } = useQuery({
    queryKey: ['detailed-wallet-balance'],
    queryFn: async () => {
      return WalletService.getDetailedBalances();
    },
    refetchInterval: refreshInterval,
  });

  const formatAmount = (msatAmount: number): string =>
    formatFromMsat(msatAmount, displayUnit, usdPerSat);

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

  const rows = (data ?? [])
    .filter(
      (detail) =>
        (detail.wallet_balance && detail.wallet_balance > 0) || detail.error
    )
    .map((detail, index) => {
      const walletMsat = convertToMsat(detail.wallet_balance || 0, detail.unit);
      const userMsat = convertToMsat(detail.user_balance || 0, detail.unit);
      const ownerMsat = convertToMsat(detail.owner_balance || 0, detail.unit);

      return {
        key: `${detail.mint_url}-${detail.unit}-${index}`,
        detail,
        walletMsat,
        userMsat,
        ownerMsat,
      };
    });

  const formatMintLabel = (detail: BalanceDetail) =>
    `${detail.mint_url.replace('https://', '').replace('http://', '')} • ${detail.unit.toUpperCase()}`;

  return (
    <>
      <Card>
        <CardHeader className='pb-4'>
          <div className='flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between'>
            <div className='space-y-1.5'>
              <CardTitle>Cashu Wallet Balance</CardTitle>
              <CardDescription>
                Detailed balance breakdown by mint and currency
              </CardDescription>
            </div>
            <div className='flex w-full gap-2 sm:w-auto'>
              <Button
                variant='default'
                size='sm'
                onClick={() => setWithdrawModalOpen(true)}
                disabled={isLoading || !data || data.length === 0}
                className='flex-1 sm:flex-none'
              >
                <Wallet className='mr-2 h-4 w-4' />
                Withdraw
              </Button>
              <Button
                variant='ghost'
                size='icon'
                onClick={() => refetch()}
                disabled={isLoading || isFetching}
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
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <div className='space-y-4'>
              <div className='grid gap-3 md:grid-cols-3'>
                {Array.from({ length: 3 }).map((_, index) => (
                  <Card key={`wallet-stat-skeleton-${index}`}>
                    <CardHeader className='space-y-2 pb-1'>
                      <Skeleton className='h-3.5 w-28' />
                      <Skeleton className='h-3 w-8' />
                    </CardHeader>
                    <CardContent className='pt-0'>
                      <Skeleton className='h-7 w-24' />
                    </CardContent>
                  </Card>
                ))}
              </div>
              <Skeleton className='h-3 w-52' />
              <div className='space-y-2'>
                {Array.from({ length: 5 }).map((_, index) => (
                  <Skeleton
                    key={`wallet-row-skeleton-${index}`}
                    className='h-11 w-full'
                  />
                ))}
              </div>
            </div>
          ) : isError ? (
            <Alert variant='destructive'>
              <AlertCircle className='h-5 w-5' />
              <AlertDescription>
                Error loading balance: {(error as Error).message}
              </AlertDescription>
            </Alert>
          ) : (
            <div className='space-y-6'>
              <div className='grid gap-3 md:grid-cols-3'>
                <Card>
                  <CardHeader className='flex flex-row items-center justify-between space-y-0 pb-2'>
                    <CardTitle className='text-muted-foreground text-sm font-medium'>
                      Your Balance (Total)
                    </CardTitle>
                    <span className='inline-flex size-8 items-center justify-center'>
                      <Coins className='size-4 text-green-600 dark:text-green-300' />
                    </span>
                  </CardHeader>
                  <CardContent className='pt-0'>
                    <p className='text-primary text-2xl font-semibold tracking-tight tabular-nums'>
                      {formatAmount(totals.totalOwner)}
                    </p>
                  </CardContent>
                </Card>
                <Card>
                  <CardHeader className='flex flex-row items-center justify-between space-y-0 pb-2'>
                    <CardTitle className='text-muted-foreground text-sm font-medium'>
                      Total Wallet
                    </CardTitle>
                    <span className='inline-flex size-8 items-center justify-center'>
                      <Wallet className='size-4 text-blue-600 dark:text-blue-300' />
                    </span>
                  </CardHeader>
                  <CardContent className='pt-0'>
                    <p className='text-2xl font-semibold tracking-tight tabular-nums'>
                      {formatAmount(totals.totalWallet)}
                    </p>
                  </CardContent>
                </Card>
                <Card>
                  <CardHeader className='flex flex-row items-center justify-between space-y-0 pb-2'>
                    <CardTitle className='text-muted-foreground text-sm font-medium'>
                      User Balance
                    </CardTitle>
                    <span className='inline-flex size-8 items-center justify-center'>
                      <User className='size-4 text-purple-600 dark:text-purple-300' />
                    </span>
                  </CardHeader>
                  <CardContent className='pt-0'>
                    <p className='text-2xl font-semibold tracking-tight tabular-nums'>
                      {formatAmount(totals.totalUser)}
                    </p>
                  </CardContent>
                </Card>
              </div>

              <p className='text-muted-foreground text-xs'>
                Your balance = Total wallet - User balance
              </p>

              {rows.length > 0 ? (
                <>
                  <div className='hidden md:block'>
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Mint / Unit</TableHead>
                          <TableHead className='text-right'>Wallet</TableHead>
                          <TableHead className='text-right'>Users</TableHead>
                          <TableHead className='text-right'>Owner</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {rows.map(
                          ({
                            key,
                            detail,
                            walletMsat,
                            userMsat,
                            ownerMsat,
                          }) => (
                            <TableRow
                              key={key}
                              className={cn(
                                detail.error &&
                                  'bg-destructive/10 text-destructive'
                              )}
                            >
                              <TableCell className='max-w-md font-mono text-xs break-all whitespace-normal'>
                                {formatMintLabel(detail)}
                              </TableCell>
                              <TableCell className='text-right font-mono'>
                                {detail.error
                                  ? 'error'
                                  : formatAmount(walletMsat)}
                              </TableCell>
                              <TableCell className='text-right font-mono'>
                                {detail.error ? '-' : formatAmount(userMsat)}
                              </TableCell>
                              <TableCell
                                className={cn(
                                  'text-right font-mono',
                                  !detail.error &&
                                    ownerMsat > 0 &&
                                    'text-primary font-semibold'
                                )}
                              >
                                {detail.error ? '-' : formatAmount(ownerMsat)}
                              </TableCell>
                            </TableRow>
                          )
                        )}
                      </TableBody>
                    </Table>
                  </div>
                  <div className='space-y-2 md:hidden'>
                    {rows.map(
                      ({ key, detail, walletMsat, userMsat, ownerMsat }) => (
                        <Card
                          key={`${key}-mobile`}
                          className={cn(
                            detail.error &&
                              'border-destructive/40 bg-destructive/5'
                          )}
                        >
                          <CardHeader className='p-4 pb-2'>
                            <CardDescription className='font-mono text-xs break-all'>
                              {formatMintLabel(detail)}
                            </CardDescription>
                          </CardHeader>
                          <CardContent className='grid grid-cols-1 gap-2 p-4 pt-0 sm:grid-cols-3 sm:gap-3'>
                            <div>
                              <p className='text-muted-foreground text-xs'>
                                Wallet
                              </p>
                              <p className='font-mono text-sm'>
                                {detail.error
                                  ? 'error'
                                  : formatAmount(walletMsat)}
                              </p>
                            </div>
                            <div>
                              <p className='text-muted-foreground text-xs'>
                                Users
                              </p>
                              <p className='font-mono text-sm'>
                                {detail.error ? '-' : formatAmount(userMsat)}
                              </p>
                            </div>
                            <div>
                              <p className='text-muted-foreground text-xs'>
                                Owner
                              </p>
                              <p
                                className={cn(
                                  'font-mono text-sm',
                                  !detail.error &&
                                    ownerMsat > 0 &&
                                    'text-primary font-semibold'
                                )}
                              >
                                {detail.error ? '-' : formatAmount(ownerMsat)}
                              </p>
                            </div>
                          </CardContent>
                        </Card>
                      )
                    )}
                  </div>
                </>
              ) : (
                <Empty className='py-6'>
                  <EmptyHeader>
                    <EmptyMedia variant='icon'>
                      <Wallet className='h-4 w-4' />
                    </EmptyMedia>
                    <EmptyTitle>No balances to display</EmptyTitle>
                    <EmptyDescription>
                      Wallet balances will appear here after funds are
                      available.
                    </EmptyDescription>
                  </EmptyHeader>
                </Empty>
              )}
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
