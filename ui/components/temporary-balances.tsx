'use client';

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  RefreshCw,
  AlertCircle,
  Key,
  Clock,
  DollarSign,
  Activity,
  type LucideIcon,
} from 'lucide-react';
import { AdminService, TemporaryBalance } from '@/lib/api/services/admin';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
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
import { cn } from '@/lib/utils';
import type { DisplayUnit } from '@/lib/types/units';
import { formatFromMsat } from '@/lib/currency';

function TemporaryBalanceStat({
  icon: Icon,
  label,
  value,
  iconClassName,
}: {
  icon: LucideIcon;
  label: string;
  value: string;
  iconClassName: string;
}) {
  return (
    <Card className='shadow-none'>
      <CardHeader className='flex flex-row items-center justify-between space-y-0 pb-2'>
        <CardTitle className='text-sm font-medium'>{label}</CardTitle>
        <span
          className={cn(
            'inline-flex size-8 items-center justify-center rounded-full',
            iconClassName
          )}
        >
          <Icon className='h-4 w-4' />
        </span>
      </CardHeader>
      <CardContent>
        <p className='text-2xl font-bold tracking-tight tabular-nums'>{value}</p>
      </CardContent>
    </Card>
  );
}

function getTotals(balances: TemporaryBalance[]) {
  let totalBalance = 0;
  let totalSpent = 0;
  let totalRequests = 0;

  balances.forEach((balance) => {
    if (!balance.parent_key_hash) {
      totalBalance += balance.balance || 0;
    }
    totalSpent += balance.total_spent || 0;
    totalRequests += balance.total_requests || 0;
  });

  return { totalBalance, totalSpent, totalRequests };
}

function buildHierarchicalData(
  allBalances: TemporaryBalance[],
  filteredBalances: TemporaryBalance[]
) {
  const parents = filteredBalances.filter((item) => !item.parent_key_hash);
  const result: Array<TemporaryBalance & { isChild?: boolean }> = [];

  parents.forEach((parent) => {
    result.push(parent);

    const children = allBalances.filter(
      (item) => item.parent_key_hash === parent.hashed_key
    );

    children.forEach((child) => {
      result.push({ ...child, isChild: true });
    });
  });

  const orphans = filteredBalances.filter(
    (item) =>
      item.parent_key_hash && !result.some((r) => r.hashed_key === item.hashed_key)
  );

  result.push(...orphans.map((item) => ({ ...item, isChild: true })));

  return result;
}

export function TemporaryBalances({
  refreshInterval = 10000,
  displayUnit,
  usdPerSat,
}: {
  refreshInterval?: number;
  displayUnit: DisplayUnit;
  usdPerSat: number | null;
}) {
  const [searchTerm, setSearchTerm] = useState('');

  const { data, isLoading, isError, error, isFetching, refetch } = useQuery({
    queryKey: ['temporary-balances'],
    queryFn: async () => AdminService.getTemporaryBalances(),
    refetchInterval: refreshInterval,
  });

  const formatBalance = (msat: number) =>
    formatFromMsat(msat, displayUnit, usdPerSat);

  const filteredData = data
    ? data.filter(
        (item) =>
          item.hashed_key.toLowerCase().includes(searchTerm.toLowerCase()) ||
          item.refund_address?.toLowerCase().includes(searchTerm.toLowerCase())
      )
    : [];

  const totals = data
    ? getTotals(data)
    : { totalBalance: 0, totalSpent: 0, totalRequests: 0 };

  const rows = data ? buildHierarchicalData(data, filteredData) : [];

  return (
    <Card className='h-full w-full shadow-sm'>
      <CardHeader className='pb-4'>
        <div className='flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between'>
          <CardTitle className='flex items-center gap-2 text-xl'>
            <Key className='h-5 w-5' />
            Temporary Balances
          </CardTitle>
          <div className='flex w-full flex-col gap-2 sm:w-auto sm:flex-row'>
            <div className='relative flex-1 sm:flex-initial'>
              <Input
                type='text'
                placeholder='Search by key or address...'
                value={searchTerm}
                onChange={(e) => setSearchTerm(e.target.value)}
                className='pr-3 pl-8 sm:w-64'
                name='temporary_balance_search'
                autoComplete='off'
              />
              <Key className='text-muted-foreground absolute top-2.5 left-2 h-4 w-4' />
            </div>
            <Button
              variant='ghost'
              size='icon'
              onClick={() => refetch()}
              disabled={isLoading || isFetching}
              className='h-8 w-full sm:w-8'
            >
              <RefreshCw
                className={cn('h-4 w-4', (isFetching || isLoading) && 'animate-spin')}
              />
              <span className='sr-only'>Refresh temporary balances</span>
            </Button>
          </div>
        </div>
        <CardDescription>
          API keys with their current balances and usage statistics
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <div className='space-y-4'>
            <div className='grid gap-3 md:grid-cols-3'>
              {Array.from({ length: 3 }).map((_, index) => (
                <Card key={`temp-stat-skeleton-${index}`} className='shadow-none'>
                  <CardHeader className='space-y-2 pb-1'>
                    <Skeleton className='h-3.5 w-24' />
                    <Skeleton className='h-3 w-8' />
                  </CardHeader>
                  <CardContent className='pt-0'>
                    <Skeleton className='h-7 w-24' />
                  </CardContent>
                </Card>
              ))}
            </div>
            <div className='space-y-2'>
              {Array.from({ length: 6 }).map((_, index) => (
                <Skeleton key={`temp-row-skeleton-${index}`} className='h-11 w-full' />
              ))}
            </div>
          </div>
        ) : isError ? (
          <Alert variant='destructive'>
            <AlertCircle className='h-5 w-5' />
            <AlertDescription>
              Error loading temporary balances: {(error as Error).message}
            </AlertDescription>
          </Alert>
        ) : (
          <div className='space-y-6'>
            <div className='grid gap-3 md:grid-cols-3'>
              <TemporaryBalanceStat
                icon={DollarSign}
                label='Total Balance'
                value={formatBalance(totals.totalBalance)}
                iconClassName='text-green-600 dark:text-green-300'
              />
              <TemporaryBalanceStat
                icon={Activity}
                label='Total Spent'
                value={formatBalance(totals.totalSpent)}
                iconClassName='text-blue-600 dark:text-blue-300'
              />
              <TemporaryBalanceStat
                icon={Key}
                label='Total Requests'
                value={totals.totalRequests.toLocaleString()}
                iconClassName='text-purple-600 dark:text-purple-300'
              />
            </div>

            {rows.length > 0 ? (
              <>
                <div className='hidden md:block'>
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Hashed Key</TableHead>
                        <TableHead className='text-right'>Balance</TableHead>
                        <TableHead className='text-right'>Total Spent</TableHead>
                        <TableHead className='text-right'>Total Requests</TableHead>
                        <TableHead>Refund Address</TableHead>
                        <TableHead className='text-right'>Expiry Time</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {rows.map((balance, index) => (
                        <TableRow
                          key={`${balance.hashed_key}-${balance.parent_key_hash ?? 'root'}-${index}`}
                          className={cn(
                            balance.balance === 0 && !balance.isChild && 'opacity-60',
                            balance.isChild && 'bg-muted/30'
                          )}
                        >
                          <TableCell className='max-w-[16rem] font-mono text-xs break-all whitespace-normal'>
                            <div className='flex items-center gap-2'>
                              {balance.isChild && (
                                <Badge variant='outline' className='h-4 px-1 text-[10px] uppercase'>
                                  Child
                                </Badge>
                              )}
                              <span>{balance.hashed_key}</span>
                            </div>
                          </TableCell>
                          <TableCell className='text-right font-mono'>
                            {balance.isChild ? (
                              <span className='text-muted-foreground italic'>(Parent)</span>
                            ) : (
                              formatBalance(balance.balance)
                            )}
                          </TableCell>
                          <TableCell className='text-right font-mono'>
                            {formatBalance(balance.total_spent)}
                          </TableCell>
                          <TableCell className='text-right font-mono'>
                            {balance.total_requests.toLocaleString()}
                          </TableCell>
                          <TableCell className='max-w-[14rem] font-mono text-xs break-all whitespace-normal'>
                            {balance.refund_address || '-'}
                          </TableCell>
                          <TableCell className='text-right font-mono text-xs'>
                            {balance.key_expiry_time ? (
                              <div className='inline-flex items-center justify-end gap-1'>
                                <Clock className='h-3 w-3' />
                                <span>
                                  {new Date(balance.key_expiry_time * 1000).toLocaleDateString()}
                                </span>
                              </div>
                            ) : (
                              '-'
                            )}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>

                <div className='space-y-2 md:hidden'>
                  {rows.map((balance, index) => (
                    <Card
                      key={`${balance.hashed_key}-${balance.parent_key_hash ?? 'root'}-mobile-${index}`}
                      className={cn(
                        'shadow-none',
                        balance.balance === 0 && !balance.isChild && 'opacity-80',
                        balance.isChild && 'bg-muted/30'
                      )}
                    >
                      <CardHeader className='p-4 pb-2'>
                        <div className='flex items-center justify-between gap-2'>
                          <CardDescription className='font-mono text-xs break-all'>
                            {balance.hashed_key}
                          </CardDescription>
                          {balance.isChild && (
                            <Badge variant='outline' className='h-4 px-1.5 text-[10px] uppercase'>
                              Child
                            </Badge>
                          )}
                        </div>
                      </CardHeader>
                      <CardContent className='grid grid-cols-2 gap-3 p-4 pt-0'>
                        <div>
                          <p className='text-muted-foreground text-xs'>Balance</p>
                          <p className='font-mono text-sm'>
                            {balance.isChild ? '(Uses Parent)' : formatBalance(balance.balance)}
                          </p>
                        </div>
                        <div>
                          <p className='text-muted-foreground text-xs'>Spent</p>
                          <p className='font-mono text-sm'>
                            {formatBalance(balance.total_spent)}
                          </p>
                        </div>
                        <div>
                          <p className='text-muted-foreground text-xs'>Requests</p>
                          <p className='font-mono text-sm'>
                            {balance.total_requests.toLocaleString()}
                          </p>
                        </div>
                        <div>
                          <p className='text-muted-foreground text-xs'>Expires</p>
                          <p className='font-mono text-xs'>
                            {balance.key_expiry_time ? (
                              <span className='inline-flex items-center gap-1'>
                                <Clock className='h-3 w-3' />
                                {new Date(balance.key_expiry_time * 1000).toLocaleDateString()}
                              </span>
                            ) : (
                              '-'
                            )}
                          </p>
                        </div>
                        {balance.refund_address && (
                          <div className='col-span-2'>
                            <p className='text-muted-foreground text-xs'>Refund Address</p>
                            <p className='font-mono text-xs break-all'>
                              {balance.refund_address}
                            </p>
                          </div>
                        )}
                      </CardContent>
                    </Card>
                  ))}
                </div>
              </>
            ) : (
              <Empty className='py-8'>
                <EmptyHeader>
                  <EmptyMedia variant='icon'>
                    {searchTerm ? (
                      <AlertCircle className='h-4 w-4' />
                    ) : (
                      <Key className='h-4 w-4' />
                    )}
                  </EmptyMedia>
                  <EmptyTitle>
                    {searchTerm
                      ? 'No temporary balances match your search'
                      : 'No temporary balances found'}
                  </EmptyTitle>
                  <EmptyDescription>
                    {searchTerm
                      ? 'Try a different key hash or refund address.'
                      : 'Temporary balances will appear here once API keys are used.'}
                  </EmptyDescription>
                </EmptyHeader>
              </Empty>
            )}

            {data && data.length > 0 && (
              <p className='text-muted-foreground text-xs'>
                Showing {filteredData.length} of {data.length} temporary balances
              </p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
