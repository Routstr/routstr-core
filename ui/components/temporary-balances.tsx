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

export function TemporaryBalances({
  refreshInterval = 10000,
}: {
  refreshInterval?: number;
}) {
  const [searchTerm, setSearchTerm] = useState('');

  const { data, isLoading, isError, error, isFetching, refetch } = useQuery({
    queryKey: ['temporary-balances'],
    queryFn: async () => {
      return AdminService.getTemporaryBalances();
    },
    refetchInterval: refreshInterval,
  });

  const formatBalance = (balance: number) => {
    return `${balance.toLocaleString()} mSats`;
  };

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
      totalBalance += balance.balance || 0;
      totalSpent += balance.total_spent || 0;
      totalRequests += balance.total_requests || 0;
    });

    return { totalBalance, totalSpent, totalRequests };
  };

  const totals = data
    ? calculateTotals(data)
    : { totalBalance: 0, totalSpent: 0, totalRequests: 0 };

  return (
    <>
      <Card className='h-full w-full shadow-sm'>
        <CardHeader className='pb-4'>
          <div className='flex items-center justify-between'>
            <CardTitle className='flex items-center gap-2 text-xl'>
              <Key className='h-5 w-5' />
              Temporary Balances
            </CardTitle>
            <div className='flex gap-2'>
              <div className='relative'>
                <input
                  type='text'
                  placeholder='Search by key or address...'
                  value={searchTerm}
                  onChange={(e) => setSearchTerm(e.target.value)}
                  className='focus:ring-primary/20 w-64 rounded-md border py-2 pr-3 pl-8 text-sm focus:ring-2 focus:outline-none'
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
                <div className='bg-muted grid grid-cols-6 gap-2 p-3 text-sm font-semibold'>
                  <div>Hashed Key</div>
                  <div className='text-right'>Balance</div>
                  <div className='text-right'>Total Spent</div>
                  <div className='text-right'>Total Requests</div>
                  <div>Refund Address</div>
                  <div className='text-right'>Expiry Time</div>
                </div>

                {filteredData.length > 0 ? (
                  filteredData.map((balance, index) => (
                    <div
                      key={index}
                      className={cn(
                        'hover:bg-muted/50 grid grid-cols-6 gap-2 border-t p-3 text-sm transition-colors',
                        balance.balance === 0 && 'opacity-60'
                      )}
                    >
                      <div className='max-w-32 truncate font-mono text-xs break-all'>
                        {balance.hashed_key}
                      </div>
                      <div className='text-right font-mono'>
                        {formatBalance(balance.balance)}
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
