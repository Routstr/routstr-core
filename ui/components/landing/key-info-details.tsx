'use client';

import { useCopyToClipboard } from '@/hooks/use-copy-to-clipboard';
import { useWalletInfo } from '@/hooks/use-wallet-info';
import type { RefundReceipt } from './cashu-payment-workflow';
import { toast } from 'sonner';
import { ApiKeyInput } from '../api-key-input';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
} from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import React, { useState, useCallback, useEffect } from 'react';
import { Copy, RefreshCcw, Trash2 } from 'lucide-react';

export type WalletSnapshot = {
  apiKey: string;
  balanceMsats: number;
  reservedMsats: number;
  totalRequests: number;
  totalSpent: number;
  validityDate: number | null;
};

interface KeyInfoDetailsProps {
  baseUrl: string;
  apiKey?: string;
  walletInfo?: WalletSnapshot | null;
  onApiKeyChanged?: (apiKey: string) => void;
  onWalletInfoUpdated?: (walletInfo: WalletSnapshot | null) => void;
  onRefundComplete?: (receipt: RefundReceipt) => void;
}

export function KeyInfoDetails({
  baseUrl,
  apiKey = '',
  walletInfo: propWalletInfo = null,
  onApiKeyChanged,
  onWalletInfoUpdated,
  onRefundComplete,
}: KeyInfoDetailsProps): React.ReactNode {
  const { copy } = useCopyToClipboard();
  const [apiKeyInput, setApiKeyInput] = useState(apiKey);
  const [isRefunding, setIsRefunding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const {
    data: queryWalletInfo,
    refetch,
    isFetching,
  } = useWalletInfo(baseUrl, apiKeyInput);
  const walletInfo = propWalletInfo ?? queryWalletInfo ?? null;

  // Sync internal state with props if they change
  useEffect(() => {
    setApiKeyInput(apiKey);
  }, [apiKey]);

  const handleRefresh = async (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (!apiKeyInput) return;
    await refetch();
  };

  const handleKeyChange = (newKey: string) => {
    setApiKeyInput(newKey);
    setError(null);
    onApiKeyChanged?.(newKey);
    // Optionally clear info when key changes
    if (newKey !== apiKey) {
      onWalletInfoUpdated?.(null);
    }
  };

  const handleCopy = async (value: string) => {
    if (await copy(value)) {
      toast.success('Copied to clipboard');
    }
  };

  const handleRefund = useCallback(async (): Promise<void> => {
    if (!apiKeyInput) {
      toast.error('Paste an API key first');
      return;
    }

    setIsRefunding(true);
    try {
      const response = await fetch(`${baseUrl}/v1/balance/refund`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${apiKeyInput}`,
        },
      });
      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(errorText || 'Refund failed');
      }
      const receipt = (await response.json()) as RefundReceipt;
      onRefundComplete?.(receipt);
      toast.success('Refund completed');
      await refetch();
    } catch (error) {
      console.error(error);
      toast.error(error instanceof Error ? error.message : 'Refund failed');
    } finally {
      setIsRefunding(false);
    }
  }, [apiKeyInput, baseUrl, onRefundComplete, refetch]);

  const formatSats = (msats: number) =>
    new Intl.NumberFormat('en-US').format(Math.floor(msats / 1000));
  const formatMsats = (msats: number) =>
    new Intl.NumberFormat('en-US').format(msats);
  const formatDate = (timestamp: number | null) =>
    timestamp ? new Date(timestamp * 1000).toLocaleDateString() : 'Never';

  return (
    <div className='space-y-6'>
      <Card>
        <CardHeader className='space-y-1'>
          <CardTitle className='text-xl'>Key Information</CardTitle>
          <CardDescription>
            Enter an API key to view its balance and consumption.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className='flex flex-col gap-2 sm:flex-row'>
            <ApiKeyInput value={apiKeyInput} onApiKeyChange={handleKeyChange} />
            <div className='flex gap-2'>
              <Button
                variant='outline'
                size='icon'
                onClick={() => handleCopy(apiKeyInput)}
                disabled={!apiKeyInput}
              >
                <Copy className='h-4 w-4' />
              </Button>
              <Button
                variant='secondary'
                size='sm'
                className='min-w-[80px] gap-1'
                onClick={handleRefresh}
                disabled={isFetching || !apiKeyInput}
                type='button'
              >
                <RefreshCcw
                  className={`h-8 w-4 ${isFetching ? 'animate-spin' : ''}`}
                />
                {isFetching ? 'Syncing...' : 'Sync'}
              </Button>
            </div>
          </div>
          {error && (
            <Alert variant='destructive' className='mt-2'>
              <AlertTitle>Error</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
        </CardContent>
      </Card>

      {walletInfo && (
        <>
          <div className='grid gap-4 md:grid-cols-2'>
            <Card>
              <CardHeader className='pb-2'>
                <CardTitle className='text-lg'>Status & Identity</CardTitle>
              </CardHeader>
              <CardContent className='space-y-4'>
                <div className='flex items-center justify-between'>
                  <span className='text-muted-foreground text-sm'>
                    Validity
                  </span>
                  <span className='text-sm font-medium'>
                    {formatDate(walletInfo.validityDate)}
                  </span>
                </div>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className='pb-2'>
                <CardTitle className='text-lg'>Infos</CardTitle>
              </CardHeader>
              <CardContent className='space-y-4'>
                <div className='flex items-center justify-between'>
                  <span className='text-muted-foreground text-sm'>
                    Spendable Balance
                  </span>
                  <span className='text-primary font-mono text-sm font-medium'>
                    {formatSats(walletInfo.balanceMsats)} sats
                  </span>
                </div>
                <div className='flex items-center justify-between'>
                  <span className='text-muted-foreground text-sm'>
                    Total Requests
                  </span>
                  <span className='font-mono text-sm font-medium'>
                    {walletInfo.totalRequests}
                  </span>
                </div>
                <div className='flex items-center justify-between'>
                  <span className='text-muted-foreground text-sm'>
                    Total Spent
                  </span>
                  <div className='text-right'>
                    <p className='font-mono text-sm font-medium'>
                      {formatSats(walletInfo.totalSpent)} sats
                    </p>
                    <p className='text-muted-foreground font-mono text-[0.6rem]'>
                      {formatMsats(walletInfo.totalSpent)} msats
                    </p>
                  </div>
                </div>
              </CardContent>
            </Card>
          </div>

          <div className='flex justify-center gap-4'>
            <Button
              onClick={handleRefund}
              disabled={isRefunding || !apiKeyInput}
              variant='destructive'
              size='sm'
              className='gap-2'
            >
              <Trash2 className='h-4 w-4' />
              {isRefunding ? 'Processing...' : 'Refund Key'}
            </Button>
          </div>
        </>
      )}
    </div>
  );
}
