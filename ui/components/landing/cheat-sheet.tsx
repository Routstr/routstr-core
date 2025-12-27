'use client';

import { type JSX, useCallback, useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Bolt, Copy, RefreshCcw, ShieldCheck, Terminal } from 'lucide-react';
import { toast } from 'sonner';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { ConfigurationService } from '@/lib/api/services/configuration';
import { CashuPaymentWorkflow } from './cashu-payment-workflow';
import { LightningPaymentWorkflow } from './lightning-payment-workflow';
import { ApiKeyManager } from './api-key-manager';

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

function normalizeBaseUrl(url: string): string {
  const trimmed = url.trim();
  if (!trimmed) {
    return '';
  }
  return trimmed.replace(/\/+$/, '');
}

export function CheatSheet(): JSX.Element {
  const [baseUrl, setBaseUrl] = useState(() =>
    typeof window === 'undefined' ? '' : ConfigurationService.getLocalBaseUrl()
  );
  const [apiKeyInput, setApiKeyInput] = useState('');
  const [walletInfo, setWalletInfo] = useState<WalletSnapshot | null>(null);
  const [refundReceipt, setRefundReceipt] = useState<RefundReceipt | null>(
    null
  );

  useEffect(() => {
    if (!baseUrl && typeof window !== 'undefined') {
      setBaseUrl(ConfigurationService.getLocalBaseUrl());
    }
  }, [baseUrl]);

  const normalizedBaseUrl = useMemo(
    () => normalizeBaseUrl(baseUrl) || DEFAULT_BASE_URL,
    [baseUrl]
  );

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

  const handleApiKeyCreated = useCallback(
    (apiKey: string, snapshot: WalletSnapshot) => {
      setApiKeyInput(apiKey);
      setWalletInfo(snapshot);
      setRefundReceipt(null);
    },
    []
  );

  const handleApiKeyChanged = useCallback((apiKey: string) => {
    setApiKeyInput(apiKey);
  }, []);

  const handleWalletInfoUpdated = useCallback((info: WalletSnapshot | null) => {
    setWalletInfo(info);
  }, []);

  const handleRefundComplete = useCallback((receipt: RefundReceipt) => {
    setRefundReceipt(receipt);
    setWalletInfo(null);
    setApiKeyInput('');
  }, []);

  const handleRefreshInfo = useCallback(async (): Promise<void> => {
    const result = await refetchNodeInfo();
    if (result.error) {
      toast.error('Unable to refresh node info');
    } else {
      toast.success('Node info refreshed');
    }
  }, [refetchNodeInfo]);

  const curlSnippet = useMemo(() => {
    const keyPreview = apiKeyInput || 'YOUR_API_KEY';
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
  }, [apiKeyInput, normalizedBaseUrl]);

  const refundToken = refundReceipt?.token ?? null;

  return (
    <div className='from-background via-background to-muted min-h-screen bg-gradient-to-b'>
      <main className='mx-auto flex w-full max-w-6xl flex-col gap-8 px-4 py-10 sm:px-6 lg:px-8'>
        <section className='relative space-y-3 text-center md:text-left'>
          <div className='absolute top-0 right-0 hidden md:block'>
            <Button asChild size='sm' className='px-3 text-xs'>
              <a href='/login'>Admin</a>
            </Button>
          </div>
          <div className='text-muted-foreground inline-flex items-center gap-2 rounded-full border px-3 py-1 text-[0.65rem] tracking-wider uppercase'>
            <Bolt className='text-primary h-4 w-4' />
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
                  <ShieldCheck className='text-primary h-4 w-4' />
                  Node identity
                </CardTitle>
                <p className='text-muted-foreground text-xs tracking-wide uppercase'>
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
                      <dt className='text-muted-foreground text-xs tracking-wide uppercase'>
                        Version
                      </dt>
                      <dd className='text-base font-medium'>
                        {nodeInfo.version}
                      </dd>
                    </div>
                    <div>
                      <dt className='text-muted-foreground text-xs tracking-wide uppercase'>
                        HTTP
                      </dt>
                      <dd className='text-base font-medium break-all'>
                        {nodeInfo.http_url || normalizedBaseUrl}
                      </dd>
                    </div>
                    {nodeInfo.onion_url && (
                      <div className='sm:col-span-2'>
                        <dt className='text-muted-foreground text-xs tracking-wide uppercase'>
                          Onion
                        </dt>
                        <dd className='text-base font-medium break-all'>
                          {nodeInfo.onion_url}
                        </dd>
                      </div>
                    )}
                    {nodeInfo.npub && (
                      <div className='sm:col-span-2'>
                        <dt className='text-muted-foreground text-xs tracking-wide uppercase'>
                          npub
                        </dt>
                        <dd className='flex items-center gap-2 font-mono text-sm break-all'>
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
                    <p className='text-muted-foreground text-xs tracking-wide uppercase'>
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
                <Terminal className='text-primary h-4 w-4' />
                Quick docs
              </CardTitle>
              <span className='text-muted-foreground text-xs tracking-wide uppercase'>
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
              <div className='bg-muted rounded-lg p-4 font-mono text-sm leading-6'>
                <pre className='break-all whitespace-pre-wrap'>
                  {curlSnippet}
                </pre>
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

        <Tabs defaultValue='cashu' className='w-full'>
          <TabsList className='grid w-full grid-cols-3'>
            <TabsTrigger value='cashu'>Cashu Payments</TabsTrigger>
            <TabsTrigger value='lightning'>Lightning Payments</TabsTrigger>
            <TabsTrigger value='manage'>Manage Keys</TabsTrigger>
          </TabsList>

          <TabsContent value='cashu' className='space-y-4'>
            <CashuPaymentWorkflow
              baseUrl={normalizedBaseUrl}
              onApiKeyCreated={handleApiKeyCreated}
            />
          </TabsContent>

          <TabsContent value='lightning' className='space-y-4'>
            <LightningPaymentWorkflow
              baseUrl={normalizedBaseUrl}
              onApiKeyCreated={handleApiKeyCreated}
            />
          </TabsContent>

          <TabsContent value='manage' className='space-y-4'>
            <ApiKeyManager
              baseUrl={normalizedBaseUrl}
              apiKey={apiKeyInput}
              walletInfo={walletInfo}
              onApiKeyChanged={handleApiKeyChanged}
              onWalletInfoUpdated={handleWalletInfoUpdated}
              onRefundComplete={handleRefundComplete}
            />

            {refundToken && (
              <Card>
                <CardHeader>
                  <CardTitle className='text-lg'>Refund Complete</CardTitle>
                </CardHeader>
                <CardContent className='space-y-2'>
                  <div className='bg-muted/30 space-y-2 rounded-lg border p-4'>
                    <div className='text-muted-foreground flex items-center justify-between text-[0.7rem] tracking-wider uppercase'>
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
                </CardContent>
              </Card>
            )}
          </TabsContent>
        </Tabs>
      </main>
    </div>
  );
}
