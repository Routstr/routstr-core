'use client';

import { useState } from 'react';
import { WalletService } from '@/lib/api/services/wallet';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Input } from '@/components/ui/input';
import {
  Key,
  Copy,
  Check,
  Loader2,
  RotateCcw,
  Plus,
  Trash2,
} from 'lucide-react';
import { toast } from 'sonner';
import { Badge } from '@/components/ui/badge';
import { KeyOptions } from './key-options';

interface KeyConfig {
  id: string;
  count: number;
  balanceLimit: string;
  balanceLimitReset: string;
  validityDate: string;
}

interface ChildKeyCreatorProps {
  baseUrl?: string;
  apiKey?: string;
  onApiKeyChange?: (apiKey: string) => void;
  costPerKeyMsats?: number;
}

export function ChildKeyCreator({
  baseUrl,
  apiKey: propApiKey,
  onApiKeyChange,
  costPerKeyMsats,
}: ChildKeyCreatorProps) {
  const [internalApiKey, setInternalApiKey] = useState('');
  const [loading, setLoading] = useState(false);
  const [configs, setConfigs] = useState<KeyConfig[]>([
    {
      id: crypto.randomUUID(),
      count: 1,
      balanceLimit: '',
      balanceLimitReset: '',
      validityDate: '',
    },
  ]);
  const [childKeyToCheck, setChildKeyToCheck] = useState('');
  const [checking, setChecking] = useState(false);
  const [keyStatus, setKeyStatus] = useState<{
    total_spent: number;
    balance_limit: number | null;
    validity_date: number | null;
    is_expired: boolean;
    is_drained: boolean;
  } | null>(null);
  const [newKeys, setNewKeys] = useState<string[]>([]);
  const [resultInfo, setResultInfo] = useState<{
    cost_msats: number;
    parent_balance: number;
  } | null>(null);
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  const activeApiKey = propApiKey ?? internalApiKey;

  const handleApiKeyChange = (val: string) => {
    setInternalApiKey(val);
    onApiKeyChange?.(val);
  };

  const addConfig = () => {
    setConfigs([
      ...configs,
      {
        id: crypto.randomUUID(),
        count: 1,
        balanceLimit: '',
        balanceLimitReset: '',
        validityDate: '',
      },
    ]);
  };

  const removeConfig = (id: string) => {
    if (configs.length > 1) {
      setConfigs(configs.filter((c) => c.id !== id));
    }
  };

  const updateConfig = (id: string, updates: Partial<KeyConfig>) => {
    setConfigs(configs.map((c) => (c.id === id ? { ...c, ...updates } : c)));
  };

  const handleCreateKey = async () => {
    if (!activeApiKey && baseUrl) {
      toast.error('Please provide a Parent API key first');
      return;
    }

    setLoading(true);
    try {
      let allNewKeys: string[] = [];
      let totalCost = 0;
      let lastParentBalance = 0;

      for (const config of configs) {
        const requestedCount = Math.max(1, Math.min(50, Number(config.count)));
        const result = await WalletService.createChildKey(
          baseUrl,
          activeApiKey,
          requestedCount,
          config.balanceLimit ? parseInt(config.balanceLimit) : undefined,
          config.balanceLimitReset || undefined,
          config.validityDate
            ? Math.floor(
                new Date(config.validityDate + 'T23:59:59').getTime() / 1000
              )
            : undefined
        );

        if (result.api_keys) {
          allNewKeys = [...allNewKeys, ...result.api_keys];
        }
        totalCost += result.cost_msats;
        lastParentBalance = result.parent_balance;
      }

      setNewKeys(allNewKeys);
      setResultInfo({
        cost_msats: totalCost,
        parent_balance: lastParentBalance,
      });

      toast.success(
        `${allNewKeys.length} child API key${
          allNewKeys.length > 1 ? 's' : ''
        } created successfully`
      );
    } catch (error) {
      console.error('Failed to create child key:', error);
      toast.error(
        error instanceof Error ? error.message : 'Failed to create child key'
      );
    } finally {
      setLoading(false);
    }
  };

  const handleCheckKey = async () => {
    if (!childKeyToCheck) {
      toast.error('Please provide a Child API key to check');
      return;
    }

    setChecking(true);
    setKeyStatus(null);
    try {
      const baseUrlToUse = baseUrl || '';
      const response = await fetch(`${baseUrlToUse}/v1/balance/info`, {
        headers: {
          Authorization: `Bearer ${childKeyToCheck}`,
        },
      });

      if (!response.ok) {
        throw new Error('Failed to fetch key info');
      }

      const info = await response.json();
      const now = Math.floor(Date.now() / 1000);

      setKeyStatus({
        total_spent: info.total_spent,
        balance_limit: info.balance_limit,
        validity_date: info.validity_date,
        is_expired: info.validity_date ? now > info.validity_date : false,
        is_drained: info.balance_limit
          ? info.total_spent >= info.balance_limit
          : false,
      });
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : 'Failed to check child key'
      );
    } finally {
      setChecking(false);
    }
  };

  const copyToClipboard = (key: string) => {
    navigator.clipboard.writeText(key);
    setCopiedKey(key);
    toast.success('API key copied to clipboard');
    setTimeout(() => setCopiedKey(null), 2000);
  };

  const copyAllToClipboard = () => {
    navigator.clipboard.writeText(newKeys.join('\n'));
    toast.success('All API keys copied to clipboard');
  };

  return (
    <div className='space-y-6'>
      <Card>
        <CardHeader>
          <div className='flex items-center justify-between'>
            <div className='space-y-1'>
              <CardTitle>Create Child API Key</CardTitle>
              <CardDescription>
                Generate secondary API keys that share your account balance.
              </CardDescription>
            </div>
            {costPerKeyMsats !== undefined && (
              <div className='text-right'>
                <p className='text-muted-foreground text-[0.65rem] tracking-wide uppercase'>
                  Unit Cost
                </p>
                <p className='text-primary text-sm font-bold'>
                  {costPerKeyMsats / 1000} sats
                </p>
              </div>
            )}
          </div>
        </CardHeader>
        <CardContent>
          <div className='space-y-4'>
            {baseUrl && (
              <div className='space-y-2'>
                <label className='text-muted-foreground text-[0.7rem] tracking-wider uppercase'>
                  Parent API Key
                </label>
                <Input
                  value={activeApiKey}
                  onChange={(e) => handleApiKeyChange(e.target.value)}
                  placeholder='sk-...'
                  className='font-mono text-sm'
                />
              </div>
            )}

            <div className='flex flex-col gap-6'>
              {configs.map((config) => (
                <div
                  key={config.id}
                  className='bg-muted/30 relative space-y-4 rounded-lg border p-4 pt-6'
                >
                  {configs.length > 1 && (
                    <Button
                      variant='ghost'
                      size='icon'
                      className='text-destructive hover:bg-destructive/10 hover:text-destructive absolute top-2 right-2 h-7 w-7'
                      onClick={() => removeConfig(config.id)}
                    >
                      <Trash2 className='h-4 w-4' />
                    </Button>
                  )}
                  <div className='flex flex-col gap-4 sm:flex-row sm:items-end'>
                    <div className='w-full space-y-2 sm:w-32'>
                      <label className='text-muted-foreground text-[0.7rem] tracking-wider uppercase'>
                        Number of keys
                      </label>
                      <Input
                        type='number'
                        min={1}
                        max={50}
                        value={config.count}
                        onChange={(e) => {
                          const val = parseInt(e.target.value);
                          updateConfig(config.id, {
                            count: isNaN(val)
                              ? 1
                              : Math.max(1, Math.min(50, val)),
                          });
                        }}
                        className='h-9'
                      />
                    </div>

                    <div className='flex-1'>
                      <KeyOptions
                        balanceLimit={config.balanceLimit}
                        setBalanceLimit={(val) =>
                          updateConfig(config.id, { balanceLimit: val })
                        }
                        validityDate={config.validityDate}
                        setValidityDate={(val) =>
                          updateConfig(config.id, { validityDate: val })
                        }
                        balanceLimitReset={config.balanceLimitReset}
                        setBalanceLimitReset={(val) =>
                          updateConfig(config.id, { balanceLimitReset: val })
                        }
                      />
                    </div>
                  </div>
                </div>
              ))}

              <div className='flex justify-center'>
                <Button
                  variant='outline'
                  size='sm'
                  onClick={addConfig}
                  className='gap-2 border-dashed'
                >
                  <Plus className='h-4 w-4' />
                  Add Another Configuration
                </Button>
              </div>

              <div className='flex flex-wrap items-center justify-between gap-4'>
                <div className='text-muted-foreground text-xs'>
                  {costPerKeyMsats && (
                    <p>
                      Total Cost:{' '}
                      <span className='text-foreground font-medium'>
                        {costPerKeyMsats *
                          configs.reduce(
                            (acc, c) => acc + Number(c.count),
                            0
                          )}{' '}
                        mSats
                      </span>
                    </p>
                  )}
                </div>

                <Button
                  onClick={handleCreateKey}
                  disabled={loading || (!!baseUrl && !activeApiKey)}
                  className='w-full min-w-[140px] sm:w-auto'
                >
                  {loading ? (
                    <>
                      <Loader2 className='mr-2 h-4 w-4 animate-spin' />
                      Creating...
                    </>
                  ) : (
                    <>
                      <Key className='mr-2 h-4 w-4' />
                      Generate{' '}
                      {configs.reduce(
                        (acc, c) => acc + Number(c.count),
                        0
                      )}{' '}
                      Keys
                    </>
                  )}
                </Button>
              </div>
            </div>

            <p className='text-muted-foreground text-xs'>
              Each key creation has a small one-time fee.
            </p>

            {newKeys.length > 0 && (
              <div className='mt-6 space-y-4'>
                <Alert className='border-green-200 bg-green-50 dark:border-green-900/20 dark:bg-green-900/10'>
                  <AlertTitle className='text-green-800 dark:text-green-400'>
                    {newKeys.length} New API Key{newKeys.length > 1 ? 's' : ''}{' '}
                    Generated
                  </AlertTitle>
                  <AlertDescription className='text-green-700 dark:text-green-500'>
                    Copy {newKeys.length > 1 ? 'these keys' : 'this key'} now.
                    You won&apos;t be able to see them again.
                    {resultInfo && (
                      <div className='mt-2 font-medium opacity-80'>
                        Total Cost: {resultInfo.cost_msats / 1000} sats | New
                        Balance: {resultInfo.parent_balance / 1000} sats
                      </div>
                    )}
                  </AlertDescription>
                </Alert>

                <div className='space-y-2'>
                  <div className='flex items-center justify-between'>
                    <span className='text-muted-foreground text-xs font-medium uppercase'>
                      Generated Keys ({newKeys.length})
                    </span>
                    {newKeys.length > 1 && (
                      <Button
                        variant='ghost'
                        size='sm'
                        className='h-7 text-[10px] uppercase'
                        onClick={copyAllToClipboard}
                      >
                        <Copy className='mr-1 h-3 w-3' />
                        Copy All
                      </Button>
                    )}
                  </div>
                  <div className='grid gap-2'>
                    {newKeys.map((key, index) => (
                      <div
                        key={index}
                        className='group relative flex items-center gap-2'
                      >
                        <code className='bg-muted/50 flex-1 rounded border p-2.5 font-mono text-[10px] break-all sm:text-xs'>
                          {key}
                        </code>
                        <Button
                          size='icon'
                          variant='ghost'
                          className='h-8 w-8 shrink-0'
                          onClick={() => copyToClipboard(key)}
                        >
                          {copiedKey === key ? (
                            <Check className='h-3.5 w-3.5 text-green-500' />
                          ) : (
                            <Copy className='h-3.5 w-3.5 opacity-50 group-hover:opacity-100' />
                          )}
                        </Button>
                      </div>
                    ))}
                  </div>
                </div>

                {newKeys.length > 3 && (
                  <div className='space-y-2'>
                    <label className='text-muted-foreground text-[0.7rem] tracking-wider uppercase'>
                      Bulk Export (All Keys)
                    </label>
                    <div className='relative'>
                      <textarea
                        readOnly
                        value={newKeys.join('\n')}
                        rows={Math.min(newKeys.length, 6)}
                        className='bg-muted/30 w-full rounded-md border p-3 font-mono text-[10px] focus:outline-none'
                      />
                      <Button
                        size='sm'
                        variant='secondary'
                        className='absolute right-2 bottom-2 h-7 text-[10px]'
                        onClick={copyAllToClipboard}
                      >
                        Copy Bulk
                      </Button>
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className='text-lg'>Check Child Key Status</CardTitle>
          <CardDescription>
            View the current spending, limit, and expiration status of any child
            key.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className='space-y-4'>
            <div className='space-y-2'>
              <label className='text-muted-foreground text-[0.7rem] tracking-wider uppercase'>
                Child API Key
              </label>
              <Input
                value={childKeyToCheck}
                onChange={(e) => setChildKeyToCheck(e.target.value)}
                placeholder='sk-...'
                className='font-mono text-sm'
              />
            </div>
            <Button
              onClick={handleCheckKey}
              disabled={checking || !childKeyToCheck}
              variant='outline'
              className='w-full'
            >
              {checking ? (
                <>
                  <Loader2 className='mr-2 h-4 w-4 animate-spin' />
                  Checking...
                </>
              ) : (
                <>
                  <RotateCcw className='mr-2 h-4 w-4' />
                  Check Status
                </>
              )}
            </Button>

            {keyStatus && (
              <div className='bg-muted/30 mt-4 space-y-3 rounded-lg border p-4 text-sm'>
                <div className='flex justify-between'>
                  <span className='text-muted-foreground'>Total Spent:</span>
                  <span className='font-mono font-medium'>
                    {keyStatus.total_spent} mSats
                  </span>
                </div>
                {keyStatus.balance_limit !== null && (
                  <div className='flex justify-between'>
                    <span className='text-muted-foreground'>Limit:</span>
                    <span className='font-mono font-medium'>
                      {keyStatus.balance_limit} mSats
                    </span>
                  </div>
                )}
                {keyStatus.validity_date !== null && (
                  <div className='flex justify-between'>
                    <span className='text-muted-foreground'>Expires:</span>
                    <span className='font-mono font-medium'>
                      {new Date(
                        keyStatus.validity_date * 1000
                      ).toLocaleDateString()}
                    </span>
                  </div>
                )}
                <div className='flex gap-2 pt-2'>
                  {keyStatus.is_drained && (
                    <Badge variant='destructive'>Drained</Badge>
                  )}
                  {keyStatus.is_expired && (
                    <Badge variant='destructive'>Expired</Badge>
                  )}
                  {!keyStatus.is_drained && !keyStatus.is_expired && (
                    <Badge className='bg-green-600 hover:bg-green-700'>
                      Active
                    </Badge>
                  )}
                </div>
              </div>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
