'use client';

import { useQuery } from '@tanstack/react-query';
import { Loader2, RefreshCw, AlertCircle } from 'lucide-react';
import { WalletService } from '@/lib/api/services/wallet';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

export function WalletBalance({
  refreshInterval = 10000,
}: {
  refreshInterval?: number;
}) {
  const { data, isLoading, isError, error, isFetching, refetch } = useQuery({
    queryKey: ['wallet-balance'],
    queryFn: async () => {
      return WalletService.getBalance();
    },
    refetchInterval: refreshInterval,
  });

  return (
    <Card className='h-full w-full shadow-sm'>
      <CardHeader className='pb-2'>
        <div className='flex items-center justify-between'>
          <CardTitle className='text-xl'>Wallet Balance</CardTitle>
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
        <CardDescription>Current available balance</CardDescription>
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
          <div className='py-6'>
            <div className='text-primary text-5xl font-bold tracking-tight'>
              {data?.balance.toLocaleString()}{' '}
              <span className='text-3xl'>sats</span>
            </div>
            <p className='text-muted-foreground mt-2 text-sm'>
              Updated {new Date().toLocaleTimeString()}
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
