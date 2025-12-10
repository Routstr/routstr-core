'use client';

import { type JSX, useCallback, useState } from 'react';
import { Copy, KeyRound, RefreshCcw, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { Input } from '@/components/ui/input';
import { Separator } from '@/components/ui/separator';

type WalletSnapshot = {
  apiKey: string;
  balanceMsats: number;
  reservedMsats: number;
};

type RefundReceipt = {
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
  };

  return {
    apiKey: payload.api_key || apiKey,
    balanceMsats: payload.balance ?? 0,
    reservedMsats: payload.reserved ?? 0,
  };
}

function formatMsats(msats: number): string {
  return new Intl.NumberFormat('en-US').format(msats);
}

function formatSats(msats: number): string {
  return new Intl.NumberFormat('en-US').format(Math.floor(msats / 1000));
}

export function CashuPaymentWorkflow({
  baseUrl,
  apiKey = '',
  walletInfo = null,
  onApiKeyCreated,
  onApiKeyChanged,
  onWalletInfoUpdated,
  onRefundComplete,
}: CashuPaymentWorkflowProps): JSX.Element {
  const [initialToken, setInitialToken] = useState('');
  const [topupToken, setTopupToken] = useState('');
  const [apiKeyInput, setApiKeyInput] = useState(apiKey);
  const [isCreatingKey, setIsCreatingKey] = useState(false);
  const [isTopupLoading, setIsTopupLoading] = useState(false);
  const [isRefunding, setIsRefunding] = useState(false);
  const [isSyncingBalance, setIsSyncingBalance] = useState(false);
  const [hasInteractedCreate, setHasInteractedCreate] = useState(false);
  const [hasInteractedManage, setHasInteractedManage] = useState(false);
  const [hasInteractedTopup, setHasInteractedTopup] = useState(false);

  const activeApiKey = apiKeyInput.trim();

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
      };
      const snapshot: WalletSnapshot = {
        apiKey: payload.api_key,
        balanceMsats: payload.balance ?? 0,
        reservedMsats: 0,
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
  }, [initialToken, baseUrl, onApiKeyCreated]);

  const handleSyncBalance = useCallback(async (): Promise<void> => {
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
  }, [activeApiKey, baseUrl, onWalletInfoUpdated]);

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
      const snapshot = await fetchWalletInfo(baseUrl, activeApiKey);
      onApiKeyCreated?.(snapshot.apiKey, snapshot);
    } catch (error) {
      console.error(error);
      toast.error(error instanceof Error ? error.message : 'Top-up failed');
    } finally {
      setIsTopupLoading(false);
    }
  }, [activeApiKey, baseUrl, topupToken, onApiKeyCreated]);

  const handleRefund = useCallback(async (): Promise<void> => {
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
      const payload = (await response.json()) as RefundReceipt;
      onRefundComplete?.(payload);
      onWalletInfoUpdated?.(null);
      setApiKeyInput('');
      toast.success('Refund requested');
    } catch (error) {
      console.error(error);
      toast.error(error instanceof Error ? error.message : 'Refund failed');
    } finally {
      setIsRefunding(false);
    }
  }, [activeApiKey, baseUrl, onRefundComplete, onWalletInfoUpdated]);

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

  const showCreateDetails =
    hasInteractedCreate || initialToken.trim().length > 0;
  const showManageDetails = hasInteractedManage || Boolean(walletInfo);
  const showTopupDetails = hasInteractedTopup || topupToken.trim().length > 0;
  const canTopup = Boolean(activeApiKey);

  return (
    <Card>
      <CardHeader className='space-y-1'>
        <CardTitle className='flex items-center gap-2 text-xl'>
          <KeyRound className='text-primary h-5 w-5' />
          API key workflow
        </CardTitle>
        <p className='text-muted-foreground text-xs tracking-wide uppercase'>
          Sections expand as soon as you interact
        </p>
      </CardHeader>
      <CardContent className='space-y-6'>
        <section className='space-y-2'>
          <header className='text-muted-foreground flex items-center justify-between text-[0.7rem] tracking-wider uppercase'>
            <span>1 · Create key</span>
            {showCreateDetails && (
              <span className='text-primary'>Cashu token detected</span>
            )}
          </header>
          <Textarea
            value={initialToken}
            onChange={(event) => setInitialToken(event.target.value)}
            placeholder='cashuA1...'
            rows={showCreateDetails ? 4 : 2}
            className='font-mono text-sm transition-all duration-200'
            onFocus={() => setHasInteractedCreate(true)}
          />
          {showCreateDetails && (
            <div className='flex flex-wrap gap-2'>
              <Button
                onClick={handleCreateKey}
                disabled={isCreatingKey}
                className='gap-2'
              >
                {isCreatingKey ? 'Creating…' : 'Create API key'}
              </Button>
              <span className='text-muted-foreground text-xs'>
                Redeems instantly and returns <code>sk-</code> key.
              </span>
            </div>
          )}
        </section>

        <Separator />

        <section className='space-y-2'>
          <header className='text-muted-foreground flex items-center justify-between text-[0.7rem] tracking-wider uppercase'>
            <span>2 · Manage key</span>
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
            <div className='grid gap-3 sm:grid-cols-2'>
              <div className='rounded-lg border p-3'>
                <p className='text-muted-foreground text-[0.65rem] tracking-wide uppercase'>
                  Spendable
                </p>
                <p className='text-xl font-semibold'>
                  {walletInfo
                    ? `${formatSats(walletInfo.balanceMsats)} sats`
                    : '—'}
                </p>
                {walletInfo && (
                  <p className='text-muted-foreground text-xs'>
                    {formatMsats(walletInfo.balanceMsats)} msats
                  </p>
                )}
              </div>
              <div className='rounded-lg border p-3'>
                <p className='text-muted-foreground text-[0.65rem] tracking-wide uppercase'>
                  Reserved
                </p>
                <p className='text-xl font-semibold'>
                  {walletInfo
                    ? `${formatSats(walletInfo.reservedMsats)} sats`
                    : '—'}
                </p>
                {walletInfo && (
                  <p className='text-muted-foreground text-xs'>
                    {formatMsats(walletInfo.reservedMsats)} msats
                  </p>
                )}
              </div>
            </div>
          )}
        </section>

        <Separator />

        <section className='space-y-2'>
          <header className='text-muted-foreground flex items-center justify-between text-[0.7rem] tracking-wider uppercase'>
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

        <Separator />

        <section className='space-y-2'>
          <header className='text-muted-foreground flex items-center justify-between text-[0.7rem] tracking-wider uppercase'>
            <span>4 · Refund</span>
          </header>
          <div className='flex flex-wrap gap-2'>
            <Button
              onClick={handleRefund}
              disabled={isRefunding || !activeApiKey}
              variant='destructive'
              className='gap-2'
            >
              <Trash2 className='h-4 w-4' />
              {isRefunding ? 'Processing…' : 'Refund remaining balance'}
            </Button>
            <span className='text-muted-foreground text-xs'>
              Burns the key and returns a fresh Cashu token.
            </span>
          </div>
        </section>
      </CardContent>
    </Card>
  );
}
