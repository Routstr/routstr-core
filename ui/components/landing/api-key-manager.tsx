'use client';

import { type JSX, useCallback, useState } from 'react';
import { Copy, RefreshCcw, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Separator } from '@/components/ui/separator';
import { WalletBalanceStats } from './wallet-balance-stats';
import type { WalletSnapshot, ChildKeyInfo } from './key-info-details';
import type { RefundReceipt } from './cashu-payment-workflow';

interface ApiKeyManagerProps {
  baseUrl: string;
  apiKey?: string;
  walletInfo?: WalletSnapshot | null;
  onApiKeyChanged?: (apiKey: string) => void;
  onWalletInfoUpdated?: (walletInfo: WalletSnapshot | null) => void;
  onRefundComplete?: (receipt: RefundReceipt) => void;
}

async function fetchWalletInfo(
  baseUrl: string,
  apiKey: string
): Promise<WalletSnapshot> {
  const response = await fetch(`${baseUrl}/v1/balance/info`, {
    cache: 'no-store',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${apiKey}`,
    },
  });

  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(errorText || 'Unable to load wallet info');
  }

  const payload = (await response.json()) as {
    api_key: string;
    balance: number;
    reserved?: number;
    is_child: boolean;
    parent_key: string | null;
    total_requests: number;
    total_spent: number;
    balance_limit: number | null;
    balance_limit_reset: string | null;
    validity_date: number | null;
    child_keys?: ChildKeyInfo[];
  };

  return {
    apiKey: payload.api_key || apiKey,
    balanceMsats: payload.balance ?? 0,
    reservedMsats: payload.reserved ?? 0,
    isChild: payload.is_child,
    parentKey: payload.parent_key,
    totalRequests: payload.total_requests,
    totalSpent: payload.total_spent,
    balanceLimit: payload.balance_limit,
    balanceLimitReset: payload.balance_limit_reset,
    validityDate: payload.validity_date,
    childKeys: payload.child_keys,
  };
}

function formatSats(msats: number): string {
  return new Intl.NumberFormat('en-US').format(Math.floor(msats / 1000));
}

export function ApiKeyManager({
  baseUrl,
  apiKey = '',
  walletInfo = null,
  onApiKeyChanged,
  onWalletInfoUpdated,
  onRefundComplete,
}: ApiKeyManagerProps): JSX.Element {
  const [apiKeyInput, setApiKeyInput] = useState(apiKey);
  const [isSyncingBalance, setIsSyncingBalance] = useState(false);
  const [isRefunding, setIsRefunding] = useState(false);
  const [hasInteractedManage, setHasInteractedManage] = useState(false);

  const handleCopy = useCallback(async (value: string): Promise<void> => {
    if (!value) {
      return;
    }
    if (typeof navigator === 'undefined' || !navigator.clipboard) {
      toast.error('Clipboard API unavailable');
      return;
    }
    try {
      await navigator.clipboard.writeText(value);
      toast.success('Copied to clipboard');
    } catch (error) {
      console.error(error);
      toast.error('Unable to copy');
    }
  }, []);

  const handleSyncBalance = useCallback(async (): Promise<void> => {
    const activeApiKey = apiKeyInput.trim();
    if (!activeApiKey) {
      toast.error('Paste an API key first');
      return;
    }

    setIsSyncingBalance(true);
    try {
      const snapshot = await fetchWalletInfo(baseUrl, activeApiKey);
      onWalletInfoUpdated?.(snapshot);
      toast.success('Balance synced');
    } catch (error) {
      console.error(error);
      toast.error(
        error instanceof Error ? error.message : 'Failed to sync balance'
      );
    } finally {
      setIsSyncingBalance(false);
    }
  }, [apiKeyInput, baseUrl, onWalletInfoUpdated]);

  const handleRefund = useCallback(async (): Promise<void> => {
    const activeApiKey = apiKeyInput.trim();
    if (!activeApiKey) {
      toast.error('Paste an API key first');
      return;
    }

    setIsRefunding(true);
    try {
      const response = await fetch(`${baseUrl}/v1/balance/refund`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${activeApiKey}`,
        },
      });
      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(errorText || 'Refund failed');
      }
      const receipt = (await response.json()) as RefundReceipt;
      onRefundComplete?.(receipt);
      onWalletInfoUpdated?.(null);
      setApiKeyInput('');
      toast.success('Refund completed');
    } catch (error) {
      console.error(error);
      toast.error(error instanceof Error ? error.message : 'Refund failed');
    } finally {
      setIsRefunding(false);
    }
  }, [apiKeyInput, baseUrl, onRefundComplete, onWalletInfoUpdated]);

  const handleApiKeyChange = useCallback(
    (newKey: string) => {
      setApiKeyInput(newKey);
      onApiKeyChanged?.(newKey);
      if (newKey !== apiKey) {
        onWalletInfoUpdated?.(null);
      }
    },
    [apiKey, onApiKeyChanged, onWalletInfoUpdated]
  );

  const activeApiKey = apiKeyInput.trim();
  const showManageDetails =
    hasInteractedManage || Boolean(walletInfo) || activeApiKey.length > 0;

  return (
    <Card>
      <CardHeader className='space-y-1'>
        <CardTitle className='flex items-center gap-2 text-xl'>
          <RefreshCcw className='text-primary h-5 w-5' />
          API Key Management
        </CardTitle>
        <p className='text-muted-foreground text-xs tracking-wide'>
          Manage your existing API keys and balances
        </p>
      </CardHeader>
      <CardContent className='space-y-6'>
        <section className='space-y-2'>
          <header className='text-muted-foreground flex items-center justify-between text-[0.7rem] tracking-wider'>
            <span>Manage existing key</span>
            {walletInfo && (
              <span className='text-primary'>
                {formatSats(walletInfo.balanceMsats)} sats
              </span>
            )}
          </header>
          <div className='flex flex-col gap-2 sm:flex-row'>
            <Input
              value={apiKeyInput}
              onChange={(event) => handleApiKeyChange(event.target.value)}
              placeholder='sk-...'
              className='font-mono text-sm'
              onFocus={() => setHasInteractedManage(true)}
            />
            <div className='flex gap-2'>
              <Button
                variant='outline'
                size='icon'
                className='h-10 w-10'
                onClick={() => handleCopy(activeApiKey)}
                disabled={!activeApiKey}
              >
                <Copy className='h-4 w-4' />
              </Button>
              <Button
                variant='secondary'
                size='sm'
                className='gap-1'
                onClick={handleSyncBalance}
                disabled={isSyncingBalance || !activeApiKey}
              >
                <RefreshCcw className='h-4 w-4' />
                Sync
              </Button>
            </div>
          </div>

          {showManageDetails && (
            <div className='space-y-4'>
              <WalletBalanceStats
                balanceMsats={walletInfo?.balanceMsats}
                reservedMsats={walletInfo?.reservedMsats}
              />

              <Separator />

              <div className='space-y-2'>
                <header className='text-muted-foreground flex items-center justify-between text-[0.7rem] tracking-wider'>
                  <span>Refund remaining balance</span>
                </header>
                <div className='flex flex-wrap gap-2'>
                  <Button
                    onClick={handleRefund}
                    disabled={isRefunding || !activeApiKey}
                    variant='destructive'
                    className='gap-2'
                  >
                    <Trash2 className='h-4 w-4' />
                    {isRefunding ? 'Processing...' : 'Refund & Delete Key'}
                  </Button>
                  <span className='text-muted-foreground text-xs'>
                    Burns the key and returns a fresh Cashu token.
                  </span>
                </div>
              </div>
            </div>
          )}
        </section>
      </CardContent>
    </Card>
  );
}
