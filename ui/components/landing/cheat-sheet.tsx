'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  Bolt,
  Copy,
  KeyRound,
  RefreshCcw,
  ShieldCheck,
  Terminal,
} from 'lucide-react';
import { toast } from 'sonner';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import { ConfigurationService } from '@/lib/api/services/configuration';

type NodeInfo = {
  name: string;
  description: string;
  version: string;
  npub?: string | null;
  mints: string[];
  http_url?: string | null;
  onion_url?: string | null;
};

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

const DEFAULT_BASE_URL = 'http://127.0.0.1:8000';

async function fetchNodeInfo(baseUrl: string): Promise<NodeInfo> {
  const response = await fetch(`${baseUrl}/v1/info`, {
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json' },
  });

  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(errorText || 'Unable to load node info');
  }

  const payload = (await response.json()) as NodeInfo;
  return {
    ...payload,
    mints: Array.isArray(payload.mints) ? payload.mints : [],
  };
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

function normalizeBaseUrl(url: string): string {
  const trimmed = url.trim();
  if (!trimmed) {
    return '';
  }
  return trimmed.replace(/\/+$/, '');
}

function formatMsats(msats: number): string {
  return new Intl.NumberFormat('en-US').format(msats);
}

function formatSats(msats: number): string {
  return new Intl.NumberFormat('en-US').format(Math.floor(msats / 1000));
}

export function CheatSheet(): JSX.Element {
  const [baseUrl, setBaseUrl] = useState(() =>
    typeof window === 'undefined' ? '' : ConfigurationService.getLocalBaseUrl()
  );
  const [initialToken, setInitialToken] = useState('');
  const [topupToken, setTopupToken] = useState('');
  const [apiKeyInput, setApiKeyInput] = useState('');
  const [walletInfo, setWalletInfo] = useState<WalletSnapshot | null>(null);
  const [refundReceipt, setRefundReceipt] = useState<RefundReceipt | null>(null);
  const [isCreatingKey, setIsCreatingKey] = useState(false);
  const [isTopupLoading, setIsTopupLoading] = useState(false);
  const [isRefunding, setIsRefunding] = useState(false);
  const [isSyncingBalance, setIsSyncingBalance] = useState(false);
  const [hasInteractedCreate, setHasInteractedCreate] = useState(false);
  const [hasInteractedManage, setHasInteractedManage] = useState(false);
  const [hasInteractedTopup, setHasInteractedTopup] = useState(false);

  useEffect(() => {
    if (!baseUrl && typeof window !== 'undefined') {
      setBaseUrl(ConfigurationService.getLocalBaseUrl());
    }
  }, [baseUrl]);

  const normalizedBaseUrl = useMemo(
    () => normalizeBaseUrl(baseUrl) || DEFAULT_BASE_URL,
    [baseUrl]
  );

  const activeApiKey = apiKeyInput.trim();

  const {
    data: nodeInfo,
    isLoading: isInfoLoading,
    isError: isInfoError,
    refetch: refetchNodeInfo,
  } = useQuery({
    queryKey: ['node-info', normalizedBaseUrl],
    queryFn: () => fetchNodeInfo(normalizedBaseUrl),
    enabled: Boolean(normalizedBaseUrl),
    refetchInterval: 300_000,
    staleTime: 120_000,
  });

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
    setRefundReceipt(null);

    try {
      const params = new URLSearchParams({
        initial_balance_token: initialToken.trim(),
      });
      const response = await fetch(
        `${normalizedBaseUrl}/v1/balance/create?${params.toString()}`,
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
      setWalletInfo(snapshot);
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
  }, [initialToken, normalizedBaseUrl]);

  const handleSyncBalance = useCallback(async (): Promise<void> => {
    if (!activeApiKey) {
      toast.error('Paste an API key first');
      return;
    }

    setIsSyncingBalance(true);
    try {
      const snapshot = await fetchWalletInfo(normalizedBaseUrl, activeApiKey);
      setWalletInfo(snapshot);
      toast.success('Balance synced');
    } catch (error) {
      console.error(error);
      toast.error(
        error instanceof Error ? error.message : 'Failed to sync balance'
      );
    } finally {
      setIsSyncingBalance(false);
    }
  }, [activeApiKey, normalizedBaseUrl]);

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
    setRefundReceipt(null);
    try {
      const response = await fetch(`${normalizedBaseUrl}/v1/balance/topup`, {
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
      const snapshot = await fetchWalletInfo(normalizedBaseUrl, activeApiKey);
      setWalletInfo(snapshot);
    } catch (error) {
      console.error(error);
      toast.error(error instanceof Error ? error.message : 'Top-up failed');
    } finally {
      setIsTopupLoading(false);
    }
  }, [activeApiKey, normalizedBaseUrl, topupToken]);

  const handleRefund = useCallback(async (): Promise<void> => {
    if (!activeApiKey) {
      toast.error('Paste an API key first');
      return;
    }

    setIsRefunding(true);
    try {
      const response = await fetch(`${normalizedBaseUrl}/v1/balance/refund`, {
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
      setRefundReceipt(payload);
      setWalletInfo(null);
      toast.success('Refund requested');
    } catch (error) {
      console.error(error);
      toast.error(error instanceof Error ? error.message : 'Refund failed');
    } finally {
      setIsRefunding(false);
    }
  }, [activeApiKey, normalizedBaseUrl]);

  const handleRefreshInfo = useCallback(async (): Promise<void> => {
    const result = await refetchNodeInfo();
    if (result.error) {
      toast.error('Unable to refresh node info');
    } else {
      toast.success('Node info refreshed');
    }
  }, [refetchNodeInfo]);

  const curlSnippet = useMemo(() => {
    const keyPreview = activeApiKey || 'YOUR_API_KEY';
    return [
      `curl -X POST "${normalizedBaseUrl}/v1/chat/completions"`,
      `  -H "Authorization: Bearer ${keyPreview}"`,
      '  -H "Content-Type: application/json"',
      "  -d '{",
      '    "model": "openai/gpt-4o-mini",',
      '    "messages": [',
      '      {"role":"system","content":"You are Routstr."},',
      '      {"role":"user","content":"Ping the node"}',
      '    ]',
      "  }'",
    ].join('\n');
  }, [activeApiKey, normalizedBaseUrl]);

  const showCreateDetails =
    hasInteractedCreate || initialToken.trim().length > 0;
  const showManageDetails = hasInteractedManage || Boolean(walletInfo);
  const showTopupDetails =
    hasInteractedTopup || topupToken.trim().length > 0;
  const refundToken = refundReceipt?.token ?? null;
  const canTopup = Boolean(activeApiKey);

  return (
    <div className='min-h-screen bg-gradient-to-b from-background via-background to-muted'>
      <main className='mx-auto flex w-full max-w-6xl flex-col gap-8 px-4 py-10 sm:px-6 lg:px-8'>
        <section className='relative space-y-3 text-center md:text-left'>
          <div className='absolute right-0 top-0 hidden md:block'>
            <Button asChild size='sm' className='px-3 text-xs'>
              <a href='/login'>Admin</a>
            </Button>
          </div>
          <div className='inline-flex items-center gap-2 rounded-full border px-3 py-1 text-[0.65rem] uppercase tracking-wider text-muted-foreground'>
            <Bolt className='h-4 w-4 text-primary' />
            Routstr cheat sheet
          </div>
          <h1 className='text-3xl font-semibold tracking-tight sm:text-4xl'>
            Node Identity and Cheat Sheet
          </h1>
          <div className='md:hidden'>
            <Button asChild size='sm' className='w-full sm:w-auto'>
              <a href='/login'>Admin</a>
            </Button>
          </div>
        </section>

        <section className='grid gap-4 lg:grid-cols-2'>
          <Card>
            <CardHeader className='flex flex-row items-start justify-between gap-4'>
              <div>
                <CardTitle className='flex items-center gap-2 text-lg'>
                  <ShieldCheck className='h-4 w-4 text-primary' />
                  Node identity
                </CardTitle>
                <p className='text-xs uppercase tracking-wide text-muted-foreground'>
                  /v1/info snapshot
                </p>
              </div>
              <Button
                variant='ghost'
                size='sm'
                className='gap-1 text-xs'
                onClick={handleRefreshInfo}
                disabled={isInfoLoading}
              >
                <RefreshCcw className='h-4 w-4' />
                Refresh
              </Button>
            </CardHeader>
            <CardContent className='space-y-4'>
              {isInfoLoading && (
                <p className='text-muted-foreground text-sm'>
                  Loading node profile…
                </p>
              )}
              {isInfoError && !isInfoLoading && (
                <p className='text-destructive text-sm'>
                  Unable to reach /v1/info at {normalizedBaseUrl}
                </p>
              )}
              {nodeInfo && (
                <>
                  <div className='space-y-2'>
                    <p className='text-2xl font-medium'>{nodeInfo.name}</p>
                    <p className='text-muted-foreground text-sm'>
                      {nodeInfo.description}
                    </p>
                  </div>
                  <dl className='grid gap-4 sm:grid-cols-2'>
                    <div>
                      <dt className='text-muted-foreground text-xs uppercase tracking-wide'>
                        Version
                      </dt>
                      <dd className='text-base font-medium'>
                        {nodeInfo.version}
                      </dd>
                    </div>
                    <div>
                      <dt className='text-muted-foreground text-xs uppercase tracking-wide'>
                        HTTP
                      </dt>
                      <dd className='text-base font-medium break-all'>
                        {nodeInfo.http_url || normalizedBaseUrl}
                      </dd>
                    </div>
                    {nodeInfo.onion_url && (
                      <div className='sm:col-span-2'>
                        <dt className='text-muted-foreground text-xs uppercase tracking-wide'>
                          Onion
                        </dt>
                        <dd className='text-base font-medium break-all'>
                          {nodeInfo.onion_url}
                        </dd>
                      </div>
                    )}
                    {nodeInfo.npub && (
                      <div className='sm:col-span-2'>
                        <dt className='text-muted-foreground text-xs uppercase tracking-wide'>
                          npub
                        </dt>
                        <dd className='flex items-center gap-2 break-all text-sm font-mono'>
                          {nodeInfo.npub}
                          <Button
                            variant='ghost'
                            size='icon'
                            className='h-8 w-8'
                            onClick={() => handleCopy(nodeInfo.npub ?? '')}
                          >
                            <Copy className='h-4 w-4' />
                          </Button>
                        </dd>
                      </div>
                    )}
                  </dl>
                  <div className='space-y-2'>
                    <p className='text-xs uppercase tracking-wide text-muted-foreground'>
                      Cashu mints
                    </p>
                    <div className='flex flex-wrap gap-2'>
                      {nodeInfo.mints.length ? (
                        nodeInfo.mints.map((mint) => (
                          <Badge
                            key={mint}
                            variant='secondary'
                            className='font-mono text-xs'
                          >
                            {mint}
                          </Badge>
                        ))
                      ) : (
                        <p className='text-muted-foreground text-sm'>
                          No mint list published
                        </p>
                      )}
                    </div>
                  </div>
                </>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className='flex flex-row items-center justify-between'>
              <CardTitle className='flex items-center gap-2 text-lg'>
                <Terminal className='h-4 w-4 text-primary' />
                Quick docs
              </CardTitle>
              <span className='text-xs uppercase tracking-wide text-muted-foreground'>
                curl-ready
              </span>
            </CardHeader>
            <CardContent className='space-y-4'>
              <div className='flex items-center gap-2'>
                <Input
                  value={baseUrl}
                  placeholder={normalizedBaseUrl}
                  onChange={(event) => setBaseUrl(event.target.value)}
                  className='text-sm'
                />
                <Button
                  variant='outline'
                  size='icon'
                  className='h-9 w-9'
                  onClick={() => handleCopy(normalizedBaseUrl)}
                >
                  <Copy className='h-4 w-4' />
                </Button>
              </div>
              <div className='rounded-lg bg-muted p-4 font-mono text-sm leading-6'>
                <pre className='whitespace-pre-wrap break-all'>{curlSnippet}</pre>
              </div>
              <div className='flex gap-2'>
                <Button
                  variant='secondary'
                  className='gap-2'
                  onClick={() => handleCopy(curlSnippet)}
                >
                  <Copy className='h-4 w-4' />
                  Copy curl
                </Button>
                <Button variant='outline' asChild className='gap-2'>
                  <a
                    href='https://docs.routstr.com'
                    target='_blank'
                    rel='noreferrer'
                  >
                    <Terminal className='h-4 w-4' />
                    Full docs
                  </a>
                </Button>
              </div>
            </CardContent>
          </Card>
        </section>

        <Card>
          <CardHeader className='space-y-1'>
            <CardTitle className='flex items-center gap-2 text-xl'>
              <KeyRound className='h-5 w-5 text-primary' />
              API key workflow
            </CardTitle>
            <p className='text-xs uppercase tracking-wide text-muted-foreground'>
              Sections expand as soon as you interact
            </p>
          </CardHeader>
          <CardContent className='space-y-6'>
            <section className='space-y-2'>
              <header className='flex items-center justify-between text-[0.7rem] uppercase tracking-wider text-muted-foreground'>
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
                  <span className='text-xs text-muted-foreground'>
                    Redeems instantly and returns <code>sk-</code> key.
                  </span>
                </div>
              )}
            </section>

            <Separator />

            <section className='space-y-2'>
              <header className='flex items-center justify-between text-[0.7rem] uppercase tracking-wider text-muted-foreground'>
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
                  onChange={(event) => {
                    setApiKeyInput(event.target.value);
                    setWalletInfo(null);
                  }}
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
                    <p className='text-[0.65rem] uppercase tracking-wide text-muted-foreground'>
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
                    <p className='text-[0.65rem] uppercase tracking-wide text-muted-foreground'>
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
              <header className='flex items-center justify-between text-[0.7rem] uppercase tracking-wider text-muted-foreground'>
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
                  <span className='text-xs text-muted-foreground'>
                    {canTopup
                      ? (
                          <>
                            Adds balance to the same <code>sk-</code> token.
                          </>
                        )
                      : 'Enter your sk- key above to unlock top ups.'}
                  </span>
                </div>
              )}
            </section>

            <Separator />

            <section className='space-y-2'>
              <header className='flex items-center justify-between text-[0.7rem] uppercase tracking-wider text-muted-foreground'>
                <span>4 · Refund</span>
                {refundReceipt && <span className='text-primary'>Done</span>}
              </header>
              <div className='flex flex-wrap gap-2'>
                <Button
                  onClick={handleRefund}
                  disabled={isRefunding}
                  variant='destructive'
                  className='gap-2'
                >
                  {isRefunding ? 'Processing…' : 'Refund remaining balance'}
                </Button>
                <span className='text-xs text-muted-foreground'>
                  Burns the key and returns a fresh Cashu token.
                </span>
              </div>
              {refundToken && (
                <div className='space-y-2 rounded-lg border bg-muted/30 p-4'>
                  <div className='flex items-center justify-between text-[0.7rem] uppercase tracking-wider text-muted-foreground'>
                    <span>Cashu refund token</span>
                    <Button
                      variant='outline'
                      size='sm'
                      className='gap-1 text-xs'
                      onClick={() => handleCopy(refundToken)}
                    >
                      <Copy className='h-3 w-3' />
                      Copy
                    </Button>
                  </div>
                  <Textarea
                    value={refundToken}
                    readOnly
                    rows={4}
                    className='font-mono text-xs'
                  />
                </div>
              )}
            </section>
          </CardContent>
        </Card>
      </main>
    </div>
  );
}

