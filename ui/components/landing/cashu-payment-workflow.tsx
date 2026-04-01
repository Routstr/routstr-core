'use client';

import { type JSX, useCallback, useState } from 'react';
import { Copy, RefreshCcw } from 'lucide-react';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { toast } from 'sonner';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { Separator } from '@/components/ui/separator';
import { KeyOptions } from '@/components/key-options';
import { ApiKeyInput } from '../api-key-input';
import type { WalletSnapshot } from './key-info-details';
import { useWalletInfo } from '@/hooks/use-wallet-info';

export type RefundReceipt = {
  token?: string;
  recipient?: string;
  sats?: string;
  msats?: string;
};

interface CashuPaymentWorkflowProps {
  baseUrl: string;
  apiKey?: string;
  walletInfo?: WalletSnapshot | null;
  onApiKeyCreated?: (apiKey: string, walletInfo: WalletSnapshot) => void;
  onApiKeyChanged?: (apiKey: string) => void;
  onWalletInfoUpdated?: (walletInfo: WalletSnapshot | null) => void;
  onRefundComplete?: (receipt: RefundReceipt) => void;
}

function formatSats(msats: number): string {
  return new Intl.NumberFormat('en-US').format(Math.floor(msats / 1000));
}

function formatMsats(msats: number): string {
  return new Intl.NumberFormat('en-US').format(msats);
}

export function CashuPaymentWorkflow({
  baseUrl,
  apiKey = '',
  walletInfo: propWalletInfo = null,
  onApiKeyCreated,
  onWalletInfoUpdated,
}: CashuPaymentWorkflowProps): JSX.Element {
  const [initialToken, setInitialToken] = useState('');
  const [topupToken, setTopupToken] = useState('');
  const [apiKeyInput, setApiKeyInput] = useState(apiKey);
  const [isCreatingKey, setIsCreatingKey] = useState(false);
  const [isTopupLoading, setIsTopupLoading] = useState(false);
  const [hasInteractedTopup, setHasInteractedTopup] = useState(false);
  const [balanceLimit, setBalanceLimit] = useState<string>('');
  const [balanceLimitReset, setBalanceLimitReset] = useState<string>('');
  const [validityDate, setValidityDate] = useState<string>('');
  const [error, setError] = useState<string | null>(null);

  const activeApiKey = apiKeyInput.trim();

  const {
    data: queryWalletInfo,
    refetch,
    isFetching,
  } = useWalletInfo(baseUrl, activeApiKey);
  const walletInfo = propWalletInfo ?? queryWalletInfo ?? null;

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

  const handleCreateKey = useCallback(async (): Promise<void> => {
    if (!initialToken.trim()) {
      toast.error('Cashu token required');
      return;
    }

    setIsCreatingKey(true);

    try {
      const params = new URLSearchParams({
        initial_balance_token: initialToken.trim(),
      });
      if (balanceLimit) params.append('balance_limit', balanceLimit);
      if (balanceLimitReset)
        params.append('balance_limit_reset', balanceLimitReset);
      if (validityDate) {
        const timestamp = Math.floor(
          new Date(validityDate + 'T23:59:59').getTime() / 1000
        );
        params.append('validity_date', timestamp.toString());
      }
      const response = await fetch(
        `${baseUrl}/v1/balance/create?${params.toString()}`,
        {
          method: 'GET',
          headers: { 'Content-Type': 'application/json' },
        }
      );
      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(errorText || 'Failed to create API key');
      }
      const payload = (await response.json()) as {
        api_key: string;
        balance: number;
        is_child: boolean;
        parent_key: string | null;
        total_requests: number;
        total_spent: number;
        balance_limit: number | null;
        balance_limit_reset: string | null;
        validity_date: number | null;
      };
      const snapshot: WalletSnapshot = {
        apiKey: payload.api_key,
        balanceMsats: payload.balance ?? 0,
        reservedMsats: 0,
        isChild: payload.is_child ?? false,
        parentKey: payload.parent_key ?? null,
        totalRequests: payload.total_requests ?? 0,
        totalSpent: payload.total_spent ?? 0,
        balanceLimit: payload.balance_limit ?? null,
        balanceLimitReset: payload.balance_limit_reset ?? null,
        validityDate: payload.validity_date ?? null,
      };

      setApiKeyInput(snapshot.apiKey);
      onApiKeyCreated?.(snapshot.apiKey, snapshot);
      setInitialToken('');
      toast.success('API key ready');
    } catch (error) {
      console.error(error);
      toast.error(
        error instanceof Error ? error.message : 'Failed to create API key'
      );
    } finally {
      setIsCreatingKey(false);
    }
  }, [
    initialToken,
    baseUrl,
    onApiKeyCreated,
    balanceLimit,
    balanceLimitReset,
    validityDate,
  ]);

  const handleSyncBalance = useCallback(async (): Promise<void> => {
    if (!activeApiKey) {
      toast.error('Paste an API key first');
      return;
    }

    setError(null);
    try {
      await refetch();
      toast.success('Balance synced');
    } catch (error) {
      console.error(error);
      const message =
        error instanceof Error ? error.message : 'Failed to sync balance';
      setError(message);
      toast.error(message);
    }
  }, [activeApiKey, refetch]);

  const handleTopup = useCallback(async (): Promise<void> => {
    if (!activeApiKey) {
      toast.error('Paste an API key first');
      return;
    }
    if (!topupToken.trim()) {
      toast.error('Cashu token required for top-up');
      return;
    }

    setIsTopupLoading(true);
    try {
      const response = await fetch(`${baseUrl}/v1/balance/topup`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${activeApiKey}`,
        },
        body: JSON.stringify({ cashu_token: topupToken.trim() }),
      });
      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(errorText || 'Failed to top up');
      }
      const payload = (await response.json()) as { msats: number };
      toast.success(`Added ${formatSats(payload.msats)} sats`);
      setTopupToken('');
      await refetch();
    } catch (error) {
      console.error(error);
      toast.error(error instanceof Error ? error.message : 'Top-up failed');
    } finally {
      setIsTopupLoading(false);
    }
  }, [activeApiKey, baseUrl, topupToken, refetch]);

  const handleApiKeyChange = useCallback(
    (newKey: string) => {
      setApiKeyInput(newKey);
      if (newKey !== apiKey) {
        onWalletInfoUpdated?.(null);
      }
    },
    [apiKey, onWalletInfoUpdated]
  );

  const showTopupDetails = hasInteractedTopup || topupToken.trim().length > 0;
  const canTopup = Boolean(activeApiKey);
  const showCreateDetails = initialToken.trim().length > 0;

  return (
    <Card>
      <CardHeader className='space-y-1'>
        <CardTitle className='text-xl'>API key workflow</CardTitle>
        <p className='text-muted-foreground text-xs tracking-wide'>
          Sections expand as soon as you interact
        </p>
      </CardHeader>
      <CardContent className='space-y-6'>
        <section className='space-y-2'>
          <header className='text-muted-foreground flex items-center justify-between text-[0.7rem] tracking-wider'>
            <span>1 · Create key</span>
            {showCreateDetails && (
              <span className='text-primary'>Cashu token detected</span>
            )}
          </header>
          <Textarea
            value={initialToken}
            onChange={(event) => setInitialToken(event.target.value)}
            placeholder='cashuA1...'
            rows={4}
            className='font-mono text-sm transition-all duration-200'
          />
          <div className='space-y-4'>
            <KeyOptions
              balanceLimit={balanceLimit}
              setBalanceLimit={setBalanceLimit}
              validityDate={validityDate}
              setValidityDate={setValidityDate}
              balanceLimitReset={balanceLimitReset}
              setBalanceLimitReset={setBalanceLimitReset}
              showBalanceLimit={false}
            />

            <div className='flex flex-wrap items-center gap-3'>
              <Button
                onClick={handleCreateKey}
                disabled={isCreatingKey}
                className='gap-2'
              >
                {isCreatingKey ? 'Creating…' : 'Create API key'}
              </Button>
              <span className='text-muted-foreground text-[0.7rem] leading-relaxed'>
                Redeems instantly and returns <code>sk-</code> key.
                <br />
                Optional limits can be set above for enhanced security.
              </span>
            </div>
          </div>
        </section>

        <Separator />

        <section className='space-y-2'>
          <header className='text-muted-foreground flex items-center justify-between text-[0.7rem] tracking-wider'>
            <span>2 · Manage key</span>
            {walletInfo && (
              <span className='text-primary'>
                {formatSats(walletInfo.balanceMsats)} sats
              </span>
            )}
          </header>
          <div className='flex flex-col gap-2 sm:flex-row'>
            <ApiKeyInput
              value={apiKeyInput}
              onApiKeyChange={handleApiKeyChange}
            />
            <div className='flex gap-2'>
              <Button
                variant='outline'
                size='icon'
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
                disabled={isFetching || !activeApiKey}
              >
                <RefreshCcw
                  className={`h-4 w-4 ${isFetching ? 'animate-spin' : ''}`}
                />
                Sync
              </Button>
            </div>
          </div>
          {walletInfo && (
            <div className='bg-muted/30 mt-2 space-y-2 rounded-lg p-3'>
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
            </div>
          )}
          {error && (
            <Alert variant='destructive' className='mt-2'>
              <AlertTitle>Error</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
        </section>

        <Separator />

        <section className='space-y-2'>
          <header className='text-muted-foreground flex items-center justify-between text-[0.7rem] tracking-wider'>
            <span>3 · Top up</span>
            {showTopupDetails && (
              <span className={canTopup ? 'text-primary' : 'text-destructive'}>
                {canTopup ? 'Ready to redeem' : 'Paste API key first'}
              </span>
            )}
          </header>
          <Textarea
            value={topupToken}
            onChange={(event) => setTopupToken(event.target.value)}
            placeholder='cashuB1...'
            rows={showTopupDetails ? 3 : 1}
            className='font-mono text-sm transition-all duration-200'
            onFocus={() => setHasInteractedTopup(true)}
            disabled={!canTopup}
          />
          {showTopupDetails && (
            <div className='flex flex-wrap gap-2'>
              <Button
                onClick={handleTopup}
                disabled={isTopupLoading || !canTopup}
                variant='outline'
                className='gap-2'
              >
                {isTopupLoading ? 'Topping up…' : 'Top up this key'}
              </Button>
              <span className='text-muted-foreground text-xs'>
                {canTopup ? (
                  <>
                    Adds balance to the same <code>sk-</code> token.
                  </>
                ) : (
                  'Enter your sk- key above to unlock top ups.'
                )}
              </span>
            </div>
          )}
        </section>
      </CardContent>
    </Card>
  );
}
