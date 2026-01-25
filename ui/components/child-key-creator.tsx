'use client';

import { useState } from 'react';
import {
  WalletService,
  CreateChildKeyResponse,
} from '@/lib/api/services/wallet';
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
import { Key, Copy, Check, Loader2 } from 'lucide-react';
import { toast } from 'sonner';

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
  const [count, setCount] = useState(1);
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

  const handleCreateKey = async () => {
    if (!activeApiKey && baseUrl) {
      toast.error('Please provide a Parent API key first');
      return;
    }

    const requestedCount = Math.max(1, Math.min(50, Number(count)));

    setLoading(true);
    try {
      const result = await WalletService.createChildKey(
        baseUrl,
        activeApiKey,
        requestedCount
      );

      console.log('Created child keys:', result);

      if (result.api_keys && result.api_keys.length > 0) {
        setNewKeys(result.api_keys);
      } else if (result.api_key) {
        setNewKeys([result.api_key]);
      } else {
        throw new Error('No API keys returned from server');
      }

      setResultInfo({
        cost_msats: result.cost_msats,
        parent_balance: result.parent_balance,
      });

      toast.success(
        `${requestedCount} child API key${
          requestedCount > 1 ? 's' : ''
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

            <div className='flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between'>
              <div className='flex-1 space-y-2'>
                <div className='flex items-center justify-between'>
                  <label className='text-muted-foreground text-[0.7rem] tracking-wider uppercase'>
                    Number of keys
                  </label>
                  {costPerKeyMsats && (
                    <span className='text-muted-foreground text-[10px]'>
                      Cost: {costPerKeyMsats * count} mSats
                    </span>
                  )}
                </div>
                <Input
                  type='number'
                  min={1}
                  max={50}
                  value={count}
                  onChange={(e) => {
                    const val = parseInt(e.target.value);
                    if (!isNaN(val)) {
                      setCount(Math.max(1, Math.min(50, val)));
                    } else {
                      setCount(1);
                    }
                  }}
                  className='w-full sm:w-24'
                />
              </div>
              <Button
                onClick={handleCreateKey}
                disabled={loading || (!!baseUrl && !activeApiKey)}
                className='w-full sm:w-auto'
              >
                {loading ? (
                  <>
                    <Loader2 className='mr-2 h-4 w-4 animate-spin' />
                    Creating...
                  </>
                ) : (
                  <>
                    <Key className='mr-2 h-4 w-4' />
                    Generate {count > 1 ? `${count} Keys` : 'Key'}
                  </>
                )}
              </Button>
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
                    You won't be able to see them again.
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
    </div>
  );
}
