'use client';

import { type JSX, useState, useCallback, useEffect } from 'react';
import {
  Copy,
  RefreshCcw,
  ShieldCheck,
  History,
  Users,
  RotateCcw,
  KeyRound,
} from 'lucide-react';
import { toast } from 'sonner';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
} from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { WalletService } from '@/lib/api/services/wallet';

export type ChildKeyInfo = {
  api_key: string;
  total_requests: number;
  total_spent: number;
  balance_limit: number | null;
  balance_limit_reset: string | null;
  validity_date: number | null;
};

export type WalletSnapshot = {
  apiKey: string;
  balanceMsats: number;
  reservedMsats: number;
  isChild: boolean;
  parentKey: string | null;
  totalRequests: number;
  totalSpent: number;
  balanceLimit: number | null;
  balanceLimitReset: string | null;
  validityDate: number | null;
  childKeys?: ChildKeyInfo[];
};

interface KeyInfoDetailsProps {
  baseUrl: string;
  apiKey?: string;
  walletInfo?: WalletSnapshot | null;
  onApiKeyChanged?: (apiKey: string) => void;
  onWalletInfoUpdated?: (walletInfo: WalletSnapshot | null) => void;
}

export function KeyInfoDetails({
  baseUrl,
  apiKey = '',
  walletInfo = null,
  onApiKeyChanged,
  onWalletInfoUpdated,
}: KeyInfoDetailsProps): JSX.Element {
  const [apiKeyInput, setApiKeyInput] = useState(apiKey);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [isResetting, setIsResetting] = useState<string | null>(null);

  // Sync internal state with props if they change
  useEffect(() => {
    setApiKeyInput(apiKey);
  }, [apiKey]);

  const fetchDetails = useCallback(
    async (keyToFetch: string) => {
      setIsRefreshing(true);
      try {
        const response = await fetch(`${baseUrl}/v1/balance/info`, {
          headers: { Authorization: `Bearer ${keyToFetch}` },
        });
        if (!response.ok) {
          throw new Error('Failed to fetch key info');
        }
        const payload = await response.json();
        const snapshot: WalletSnapshot = {
          apiKey: payload.api_key || keyToFetch,
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
        onWalletInfoUpdated?.(snapshot);
        toast.success('Key details synced');
      } catch (error) {
        toast.error(
          error instanceof Error ? error.message : 'Failed to fetch details'
        );
      } finally {
        setIsRefreshing(false);
      }
    },
    [baseUrl, onWalletInfoUpdated]
  );

  const handleRefresh = async () => {
    if (!apiKeyInput) return;
    await fetchDetails(apiKeyInput);
  };

  const handleKeyChange = (newKey: string) => {
    setApiKeyInput(newKey);
    onApiKeyChanged?.(newKey);
    // Optionally clear info when key changes
    if (newKey !== apiKey) {
      onWalletInfoUpdated?.(null);
    }
  };

  const handleCopy = (value: string) => {
    navigator.clipboard.writeText(value);
    toast.success('Copied to clipboard');
  };

  const handleResetSpent = async (childKey: string) => {
    if (!walletInfo || walletInfo.isChild) return;

    setIsResetting(childKey);
    try {
      await WalletService.resetChildKeySpent(baseUrl, apiKeyInput, childKey);
      toast.success('Child key spent reset');
      await fetchDetails(apiKeyInput);
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : 'Failed to reset child key'
      );
    } finally {
      setIsResetting(null);
    }
  };

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
          <CardTitle className='flex items-center gap-2 text-xl'>
            <KeyRound className='text-primary h-5 w-5' />
            Key Information
          </CardTitle>
          <CardDescription>
            Enter an API key to view its balance, consumption, and child keys.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className='flex flex-col gap-2 sm:flex-row'>
            <Input
              value={apiKeyInput}
              onChange={(e) => handleKeyChange(e.target.value)}
              placeholder='sk-...'
              className='font-mono text-sm'
            />
            <div className='flex gap-2'>
              <Button
                variant='outline'
                size='icon'
                className='h-10 w-10 shrink-0'
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
                disabled={isRefreshing || !apiKeyInput}
              >
                <RefreshCcw
                  className={`h-4 w-4 ${isRefreshing ? 'animate-spin' : ''}`}
                />
                {isRefreshing ? 'Syncing...' : 'Sync'}
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      {walletInfo && (
        <>
          <div className='grid gap-4 md:grid-cols-2'>
            <Card>
              <CardHeader className='pb-2'>
                <CardTitle className='flex items-center gap-2 text-lg'>
                  <ShieldCheck className='text-primary h-5 w-5' />
                  Status & Identity
                </CardTitle>
              </CardHeader>
              <CardContent className='space-y-4'>
                <div className='flex items-center justify-between'>
                  <span className='text-muted-foreground text-sm'>Type</span>
                  <Badge variant={walletInfo.isChild ? 'secondary' : 'default'}>
                    {walletInfo.isChild ? 'Child Key' : 'Parent Key'}
                  </Badge>
                </div>
                {walletInfo.parentKey && (
                  <div className='space-y-1'>
                    <span className='text-muted-foreground text-xs tracking-wider uppercase'>
                      Parent Key
                    </span>
                    <div className='flex items-center gap-2'>
                      <code className='bg-muted flex-1 rounded px-2 py-1 font-mono text-xs break-all'>
                        {walletInfo.parentKey}
                      </code>
                      <Button
                        variant='ghost'
                        size='icon'
                        className='h-8 w-8'
                        onClick={() => handleCopy(walletInfo.parentKey!)}
                      >
                        <Copy className='h-4 w-4' />
                      </Button>
                    </div>
                  </div>
                )}
                <div className='flex items-center justify-between'>
                  <span className='text-muted-foreground text-sm'>
                    Validity
                  </span>
                  <span className='text-sm font-medium'>
                    {formatDate(walletInfo.validityDate)}
                  </span>
                </div>
                <div className='flex items-center justify-between'>
                  <span className='text-muted-foreground text-sm'>
                    Spendable Balance
                  </span>
                  <span className='text-primary font-mono text-sm font-medium'>
                    {formatSats(walletInfo.balanceMsats)} sats
                  </span>
                </div>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className='pb-2'>
                <CardTitle className='flex items-center gap-2 text-lg'>
                  <History className='text-primary h-5 w-5' />
                  Consumption
                </CardTitle>
              </CardHeader>
              <CardContent className='space-y-4'>
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
                {walletInfo.balanceLimit !== null && (
                  <div className='space-y-2'>
                    <div className='flex items-center justify-between'>
                      <span className='text-muted-foreground text-sm'>
                        Spend Limit
                      </span>
                      <span className='font-mono text-sm font-medium'>
                        {formatSats(walletInfo.balanceLimit)} sats
                      </span>
                    </div>
                    {walletInfo.balanceLimitReset && (
                      <div className='flex items-center justify-between'>
                        <span className='text-muted-foreground text-sm'>
                          Reset Policy
                        </span>
                        <Badge variant='outline' className='capitalize'>
                          {walletInfo.balanceLimitReset}
                        </Badge>
                      </div>
                    )}
                  </div>
                )}
              </CardContent>
            </Card>
          </div>

          {!walletInfo.isChild &&
            walletInfo.childKeys &&
            walletInfo.childKeys.length > 0 && (
              <Card>
                <CardHeader>
                  <CardTitle className='flex items-center gap-2 text-lg'>
                    <Users className='text-primary h-5 w-5' />
                    Child Keys ({walletInfo.childKeys.length})
                  </CardTitle>
                  <CardDescription>
                    Secondary keys using this account&apos;s balance
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  <div className='space-y-4'>
                    {walletInfo.childKeys.map((ck) => (
                      <div
                        key={ck.api_key}
                        className='space-y-3 rounded-lg border p-4'
                      >
                        <div className='flex items-center justify-between gap-4'>
                          <code className='bg-muted flex-1 rounded px-2 py-1 font-mono text-xs break-all'>
                            {ck.api_key}
                          </code>
                          <div className='flex gap-1'>
                            <Button
                              variant='ghost'
                              size='icon'
                              className='h-8 w-8'
                              onClick={() => handleCopy(ck.api_key)}
                            >
                              <Copy className='h-4 w-4' />
                            </Button>
                            <Button
                              variant='ghost'
                              size='icon'
                              className='text-destructive h-8 w-8'
                              title='Reset consumption'
                              disabled={isResetting === ck.api_key}
                              onClick={() => handleResetSpent(ck.api_key)}
                            >
                              {isResetting === ck.api_key ? (
                                <RefreshCcw className='h-4 w-4 animate-spin' />
                              ) : (
                                <RotateCcw className='h-4 w-4' />
                              )}
                            </Button>
                          </div>
                        </div>
                        <div className='grid grid-cols-2 gap-4 text-xs sm:grid-cols-5'>
                          <div>
                            <p className='text-muted-foreground text-[0.6rem] tracking-wider uppercase'>
                              Requests
                            </p>
                            <p className='font-mono font-medium'>
                              {ck.total_requests}
                            </p>
                          </div>
                          <div>
                            <p className='text-muted-foreground text-[0.6rem] tracking-wider uppercase'>
                              Spent
                            </p>
                            <p className='font-mono font-medium'>
                              {formatSats(ck.total_spent)} sats
                            </p>
                          </div>
                          <div>
                            <p className='text-muted-foreground text-[0.6rem] tracking-wider uppercase'>
                              Limit
                            </p>
                            <p className='font-mono font-medium'>
                              {ck.balance_limit
                                ? `${formatSats(ck.balance_limit)} sats`
                                : 'None'}
                            </p>
                          </div>
                          <div>
                            <p className='text-muted-foreground text-[0.6rem] tracking-wider uppercase'>
                              Policy
                            </p>
                            <p className='font-medium capitalize'>
                              {ck.balance_limit_reset || 'None'}
                            </p>
                          </div>
                          <div>
                            <p className='text-muted-foreground text-[0.6rem] tracking-wider uppercase'>
                              Expires
                            </p>
                            <p className='font-medium'>
                              {formatDate(ck.validity_date)}
                            </p>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </CardContent>
              </Card>
            )}

          <div className='flex justify-center'>
            <Button
              variant='ghost'
              size='sm'
              onClick={handleRefresh}
              disabled={isRefreshing}
              className='text-muted-foreground'
            >
              <RefreshCcw
                className={`mr-2 h-3 w-3 ${isRefreshing ? 'animate-spin' : ''}`}
              />
              Last synced: {new Date().toLocaleTimeString()}
            </Button>
          </div>
        </>
      )}
    </div>
  );
}
