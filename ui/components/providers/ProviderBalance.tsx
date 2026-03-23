'use client';

import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { AdminService } from '@/lib/api/services/admin';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Skeleton } from '@/components/ui/skeleton';
import { Separator } from '@/components/ui/separator';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { SimpleLightningTopup } from './SimpleLightningTopup';
import { SimpleCashuTopup } from './SimpleCashuTopup';

interface ProviderBalanceProps {
  providerId: number;
  platformUrl?: string | null;
  isRoutstr?: boolean;
  nodeUrl?: string;
}

export function ProviderBalance({
  providerId,
  platformUrl,
  isRoutstr = false,
  nodeUrl,
}: ProviderBalanceProps) {
  const [isTopupDialogOpen, setIsTopupDialogOpen] = useState(false);
  const [isHovered, setIsHovered] = useState(false);
  const queryClient = useQueryClient();

  const {
    data: balanceData,
    isLoading,
    error,
  } = useQuery({
    queryKey: ['provider-balance', providerId],
    queryFn: () => AdminService.getProviderBalance(providerId),
    refetchInterval: 30000,
    refetchOnWindowFocus: true,
    retry: 1,
  });

  const handleTopUpClick = () => {
    if (
      platformUrl &&
      (platformUrl.includes('openrouter.ai') ||
        platformUrl.includes('openai.com'))
    ) {
      window.open(platformUrl, '_blank');
      return;
    }

    setIsTopupDialogOpen(true);
  };

  const handleCloseDialog = () => {
    setIsTopupDialogOpen(false);
    queryClient.invalidateQueries({
      queryKey: ['provider-balance', providerId],
    });
  };

  if (isLoading) {
    return <Skeleton className='h-9 w-24' />;
  }

  if (
    error ||
    !balanceData?.ok ||
    balanceData.balance_data === undefined ||
    balanceData.balance_data === null
  ) {
    return null;
  }

  const balance = balanceData.balance_data;
  let displayValue = 'N/A';

  if (typeof balance === 'number') {
    displayValue = isRoutstr
      ? `${balance.toLocaleString()} sats`
      : `$${balance.toFixed(2)}`;
  } else if (balance && typeof balance === 'object') {
    const b = balance as Record<string, unknown>;
    if (typeof b.balance === 'number') {
      displayValue = `$${b.balance.toFixed(2)}`;
    } else if (typeof b.balance === 'string') {
      displayValue = b.balance;
    } else if (b.amount !== undefined) {
      displayValue = `$${Number(b.amount).toFixed(2)}`;
    }
  }

  return (
    <>
      <Button
        variant='outline'
        size='sm'
        onClick={handleTopUpClick}
        onMouseEnter={() => setIsHovered(true)}
        onMouseLeave={() => setIsHovered(false)}
        className='w-full font-mono sm:w-auto'
      >
        {isHovered ? 'Top Up' : displayValue}
      </Button>

      <Dialog open={isTopupDialogOpen} onOpenChange={handleCloseDialog}>
        <DialogContent className='max-h-[90vh] overflow-y-auto sm:max-w-md'>
          <DialogHeader>
            <DialogTitle>Top Up Balance</DialogTitle>
            <DialogDescription>
              {isRoutstr
                ? `Top up your balance on node ${nodeUrl}`
                : 'Choose a payment method to top up your account balance.'}
            </DialogDescription>
          </DialogHeader>

          <div className='space-y-6 py-4'>
            <section className='space-y-2'>
              <Label className='text-muted-foreground text-xs font-semibold tracking-wider uppercase'>
                Lightning Top-up
              </Label>
              <SimpleLightningTopup
                providerId={providerId}
                baseUrl={nodeUrl || ''}
                onSuccess={() => {
                  queryClient.invalidateQueries({
                    queryKey: ['provider-balance', providerId],
                  });
                }}
              />
            </section>

            <Separator />

            <section className='space-y-2'>
              <Label className='text-muted-foreground text-xs font-semibold tracking-wider uppercase'>
                Cashu Token Top-up
              </Label>
              <SimpleCashuTopup
                providerId={providerId}
                baseUrl={nodeUrl || ''}
                onSuccess={() => {
                  queryClient.invalidateQueries({
                    queryKey: ['provider-balance', providerId],
                  });
                }}
              />
            </section>
          </div>

          <DialogFooter>
            <Button
              variant='outline'
              onClick={handleCloseDialog}
              className='w-full'
            >
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
