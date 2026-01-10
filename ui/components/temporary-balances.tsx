'use client';

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  Loader2,
  RefreshCw,
  AlertCircle,
  Key,
  Clock,
  DollarSign,
  Activity,
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
import { cn } from '@/lib/utils';
import type { DisplayUnit } from '@/lib/types/units';
import { formatFromMsat } from '@/lib/currency';

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
    queryFn: async () => {
      return AdminService.getTemporaryBalances();
    },
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

  const calculateTotals = (balances: TemporaryBalance[]) => {
    let totalBalance = 0;
    let totalSpent = 0;
    let totalRequests = 0;

    balances.forEach((balance) => {
      // Only count parents for total balance to avoid double counting
      // since child keys use parent balance
      if (!balance.parent_key_hash) {
        totalBalance += balance.balance || 0;
      }
      totalSpent += balance.total_spent || 0;
      totalRequests += balance.total_requests || 0;
    });

    return { totalBalance, totalSpent, totalRequests };
  };

  const totals = data
    ? calculateTotals(data)
    : { totalBalance: 0, totalSpent: 0, totalRequests: 0 };

  // Group parents and children
  const hierarchicalData = (() => {
    if (!data) return [];

    const parents = filteredData.filter((item) => !item.parent_key_hash);
    const result: (TemporaryBalance & { isChild?: boolean })[] = [];

    parents.forEach((parent) => {
      result.push(parent);
      const children = data.filter(
        (item) => item.parent_key_hash === parent.hashed_key
      );
      children.forEach((child) => {
        result.push({ ...child, isChild: true });
      });
    });

    // Add children whose parents didn't match the search or aren't in the list
    const orphans = filteredData.filter(
      (item) =>
        item.parent_key_hash &&
        !result.some((r) => r.hashed_key === item.hashed_key)
    );
    result.push(...orphans.map((o) => ({ ...o, isChild: true })));

    return result;
  })();

  return (
    <>
      <Card className='h-full w-full shadow-sm'>
        <CardHeader className='pb-4'>
          <div className='flex items-center justify-between'>
            <CardTitle className='flex items-center gap-2 text-xl'>
              <Key className='h-5 w-5' />
              Temporary Balances
            </CardTitle>
            <div className='flex flex-col gap-2 sm:flex-row sm:gap-2'>
              <div className='relative flex-1 sm:flex-initial'>
                <input
                  type='text'
                  placeholder='Search by key or address...'
                  value={searchTerm}
                  onChange={(e) => setSearchTerm(e.target.value)}
                  className='focus:ring-primary/20 w-full rounded-md border py-2 pr-3 pl-8 text-sm focus:ring-2 focus:outline-none sm:w-64'
                />
                <Key className='text-muted-foreground absolute top-2.5 left-2 h-4 w-4' />
              </div>
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
            <div className='flex items-center justify-center py-8'>
              <Loader2 className='text-primary h-8 w-8 animate-spin' />
            </div>
          ) : isError ? (
            <div className='bg-destructive/10 text-destructive flex items-center space-x-2 rounded-md p-4'>
              <AlertCircle className='h-5 w-5' />
              <span>
                Error loading temporary balances: {(error as Error).message}
              </span>
            </div>
          ) : (
            <div className='space-y-6'>
              {/* Summary Cards */}
              <div className='mb-6 grid grid-cols-1 gap-4 md:grid-cols-3'>
                <div className='rounded-lg border border-blue-200 bg-gradient-to-r from-blue-50 to-indigo-50 p-4'>
                  <div className='flex items-center justify-between'>
                    <div className='flex items-center gap-2'>
                      <DollarSign className='h-5 w-5 text-blue-600' />
                      <span className='text-sm font-medium text-blue-800'>
                        Total Balance
                      </span>
                    </div>
                  </div>
                  <div className='mt-2 text-2xl font-bold text-blue-900'>
                    {formatBalance(totals.totalBalance)}
                  </div>
                </div>
                <div className='rounded-lg border border-green-200 bg-gradient-to-r from-green-50 to-emerald-50 p-4'>
                  <div className='flex items-center justify-between'>
                    <div className='flex items-center gap-2'>
                      <Activity className='h-5 w-5 text-green-600' />
                      <span className='text-sm font-medium text-green-800'>
                        Total Spent
                      </span>
                    </div>
                  </div>
                  <div className='mt-2 text-2xl font-bold text-green-900'>
                    {formatBalance(totals.totalSpent)}
                  </div>
                </div>
                <div className='rounded-lg border border-purple-200 bg-gradient-to-r from-purple-50 to-pink-50 p-4'>
                  <div className='flex items-center justify-between'>
                    <div className='flex items-center gap-2'>
                      <Key className='h-5 w-5 text-purple-600' />
                      <span className='text-sm font-medium text-purple-800'>
                        Total Requests
                      </span>
                    </div>
                  </div>
                  <div className='mt-2 text-2xl font-bold text-purple-900'>
                    {totals.totalRequests.toLocaleString()}
                  </div>
                </div>
              </div>

              {/* Table */}
              <div className='overflow-hidden rounded-lg border'>
                {/* Desktop Table Header */}
                <div className='bg-muted hidden grid-cols-6 gap-2 p-3 text-sm font-semibold md:grid'>
                  <div>Hashed Key</div>
                  <div className='text-right'>Balance</div>
                  <div className='text-right'>Total Spent</div>
                  <div className='text-right'>Total Requests</div>
                  <div>Refund Address</div>
                  <div className='text-right'>Expiry Time</div>
                </div>

                {hierarchicalData.length > 0 ? (
                  hierarchicalData.map((balance, index) => (
                    <div
                      key={index}
                      className={cn(
                        'hover:bg-muted/50 border-t p-3 text-sm transition-colors',
                        balance.balance === 0 && !balance.isChild && 'opacity-60',
                        balance.isChild && 'bg-blue-50/30 ml-4 border-l-2 border-l-blue-200'
                      )}
                    >
                      {/* Desktop Layout */}
                      <div className='hidden grid-cols-6 gap-2 md:grid'>
                        <div className='flex max-w-48 items-center gap-2 truncate font-mono text-xs break-all'>
                          {balance.isChild && (
                            <span className='bg-blue-100 text-blue-700 rounded px-1 py-0.5 text-[10px] font-bold uppercase'>
                              Child
                            </span>
                          )}
                          {balance.hashed_key}
                        </div>
                        <div className='text-right font-mono'>
                          {balance.isChild ? (
                            <span className='text-muted-foreground italic'>(Parent)</span>
                          ) : (
                            formatBalance(balance.balance)
                          )}
                        </div>
                        <div className='text-right font-mono'>
                          {formatBalance(balance.total_spent)}
                        </div>
                        <div className='text-right font-mono'>
                          {balance.total_requests.toLocaleString()}
                        </div>
                        <div className='max-w-32 truncate font-mono text-xs break-all'>
                          {balance.refund_address || '-'}
                        </div>
                        <div className='text-right font-mono text-xs'>
                          {balance.key_expiry_time ? (
                            <div className='flex items-center justify-end gap-1'>
                              <Clock className='h-3 w-3' />
                              <span>
                                {new Date(
                                  balance.key_expiry_time * 1000
                                ).toLocaleDateString()}
                              </span>
                            </div>
                          ) : (
                            '-'
                          )}
                        </div>
                      </div>

                      {/* Mobile Layout */}
                      <div className='space-y-3 md:hidden'>
                        <div className='flex items-center justify-between'>
                          <div className='space-y-1'>
                            <span className='text-muted-foreground text-xs font-medium'>
                              {balance.isChild ? 'Child Key' : 'Key'}
                            </span>
                            <div className='font-mono text-xs break-all'>
                              {balance.hashed_key}
                            </div>
                          </div>
                          {balance.isChild && (
                            <span className='bg-blue-100 text-blue-700 rounded px-1.5 py-0.5 text-[10px] font-bold uppercase'>
                              Child
                            </span>
                          )}
                        </div>

                        <div className='grid grid-cols-2 gap-3'>
                          <div className='space-y-1'>
                            <div className='text-muted-foreground text-xs font-medium'>
                              Balance
                            </div>
                            <div className='truncate font-mono text-sm'>
                              {balance.isChild ? (
                                <span className='text-muted-foreground italic text-xs'>(Uses Parent)</span>
                              ) : (
                                formatBalance(balance.balance)
                              )}
                            </div>
                          </div>
                          <div className='space-y-1'>
                            <div className='text-muted-foreground text-xs font-medium'>
                              Spent
                            </div>
                            <div className='truncate font-mono text-sm'>
                              {formatBalance(balance.total_spent)}
                            </div>
                          </div>
                        </div>

                        <div className='grid grid-cols-2 gap-3'>
                          <div className='space-y-1'>
                            <div className='text-muted-foreground text-xs font-medium'>
                              Requests
                            </div>
                            <div className='truncate font-mono text-sm'>
                              {balance.total_requests.toLocaleString()}
                            </div>
                          </div>
                          <div className='space-y-1'>
                            <div className='text-muted-foreground text-xs font-medium'>
                              Expires
                            </div>
                            <div className='font-mono text-xs'>
                              {balance.key_expiry_time ? (
                                <div className='flex items-center gap-1'>
                                  <Clock className='h-3 w-3 flex-shrink-0' />
                                  <span className='truncate'>
                                    {new Date(
                                      balance.key_expiry_time * 1000
                                    ).toLocaleDateString()}
                                  </span>
                                </div>
                              ) : (
                                '-'
                              )}
                            </div>
                          </div>
                        </div>

                        {balance.refund_address && (
                          <div className='space-y-1 border-t pt-2'>
                            <div className='text-muted-foreground text-xs font-medium'>
                              Refund Address
                            </div>
                            <div className='font-mono text-xs break-all'>
                              {balance.refund_address}
                            </div>
                          </div>
                        )}
                      </div>
                    </div>
                  ))
                ) : (
                  <div className='text-muted-foreground p-8 text-center text-sm'>
                    {searchTerm ? (
                      <div className='flex flex-col items-center gap-2'>
                        <AlertCircle className='h-8 w-8' />
                        <span>No temporary balances match your search</span>
                      </div>
                    ) : (
                      <div className='flex flex-col items-center gap-2'>
                        <Key className='h-8 w-8' />
                        <span>No temporary balances found</span>
                      </div>
                    )}
                  </div>
                )}
              </div>

              {data && data.length > 0 && (
                <div className='text-muted-foreground mt-4 text-xs'>
                  Showing {filteredData.length} of {data.length} temporary
                  balances
                </div>
              )}
            </div>
          )}
        </CardContent>
      </Card>
    </>
  );
}
