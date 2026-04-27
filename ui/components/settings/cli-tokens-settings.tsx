'use client';

import * as React from 'react';
import { useState, useEffect, useCallback } from 'react';
import {
  AdminService,
  type CliTokenListItem,
  type CliTokenCreated,
} from '@/lib/api/services/admin';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
} from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Skeleton } from '@/components/ui/skeleton';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { AlertCircle, Copy, Trash2, Check } from 'lucide-react';
import { toast } from 'sonner';

function formatTs(ts: number | null): string {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString();
}

export function CliTokensSettings(): React.ReactElement {
  const [tokens, setTokens] = useState<CliTokenListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState('');
  const [expiresInDays, setExpiresInDays] = useState<string>('');
  const [creating, setCreating] = useState(false);
  const [newToken, setNewToken] = useState<CliTokenCreated | null>(null);
  const [copied, setCopied] = useState(false);

  const loadTokens = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      const data = await AdminService.listCliTokens();
      setTokens(data);
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : 'Failed to load tokens';
      setError(message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadTokens();
  }, [loadTokens]);

  async function handleCreate(): Promise<void> {
    const trimmed = name.trim();
    if (!trimmed) {
      toast.error('Name is required');
      return;
    }
    const days = expiresInDays.trim()
      ? Number.parseInt(expiresInDays.trim(), 10)
      : undefined;
    if (days !== undefined && (Number.isNaN(days) || days <= 0)) {
      toast.error('Expiry must be a positive number of days');
      return;
    }

    setCreating(true);
    try {
      const created = await AdminService.createCliToken(trimmed, days);
      setNewToken(created);
      setName('');
      setExpiresInDays('');
      await loadTokens();
      toast.success('Token created. Copy it now — it will not be shown again.');
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : 'Failed to create token';
      toast.error(message);
    } finally {
      setCreating(false);
    }
  }

  async function handleRevoke(id: string): Promise<void> {
    if (
      !confirm('Revoke this token? Any CLI/agent using it will lose access.')
    ) {
      return;
    }
    try {
      await AdminService.revokeCliToken(id);
      await loadTokens();
      toast.success('Token revoked');
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : 'Failed to revoke token';
      toast.error(message);
    }
  }

  async function handleCopy(): Promise<void> {
    if (!newToken) return;
    await navigator.clipboard.writeText(newToken.token);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  return (
    <div className='space-y-6'>
      <Card>
        <CardHeader>
          <CardTitle>Create CLI Token</CardTitle>
          <CardDescription>
            Generate a long-lived bearer token for the Routstr CLI or AI agents.
            Use this token in <code>~/.routstr/config.json</code> or with{' '}
            <code>routstr init --token &lt;token&gt;</code>.
          </CardDescription>
        </CardHeader>
        <CardContent className='space-y-4'>
          {newToken && (
            <Alert className='border-green-500/50 bg-green-500/10'>
              <AlertDescription className='space-y-3'>
                <div className='font-medium text-green-700 dark:text-green-400'>
                  Token created. Copy it now — it will not be shown again.
                </div>
                <div className='flex items-center gap-2'>
                  <code className='bg-muted flex-1 rounded px-3 py-2 text-xs break-all'>
                    {newToken.token}
                  </code>
                  <Button
                    type='button'
                    variant='outline'
                    size='sm'
                    onClick={handleCopy}
                  >
                    {copied ? (
                      <Check className='h-4 w-4' />
                    ) : (
                      <Copy className='h-4 w-4' />
                    )}
                  </Button>
                </div>
                <Button
                  type='button'
                  variant='ghost'
                  size='sm'
                  onClick={() => setNewToken(null)}
                >
                  Dismiss
                </Button>
              </AlertDescription>
            </Alert>
          )}
          <div className='grid grid-cols-1 gap-4 md:grid-cols-2'>
            <div className='space-y-2'>
              <Label htmlFor='cli-token-name'>Name</Label>
              <Input
                id='cli-token-name'
                placeholder='e.g. dev-laptop, ci-runner'
                value={name}
                onChange={(e) => setName(e.target.value)}
                disabled={creating}
              />
            </div>
            <div className='space-y-2'>
              <Label htmlFor='cli-token-expiry'>
                Expires in days (optional)
              </Label>
              <Input
                id='cli-token-expiry'
                type='number'
                min='1'
                placeholder='Never expires if blank'
                value={expiresInDays}
                onChange={(e) => setExpiresInDays(e.target.value)}
                disabled={creating}
              />
            </div>
          </div>
          <Button onClick={handleCreate} disabled={creating || !name.trim()}>
            {creating ? 'Creating…' : 'Create Token'}
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Active Tokens</CardTitle>
          <CardDescription>
            Tokens authorize CLI/agent calls to admin endpoints. Revoke any
            token that may have been exposed.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {error && (
            <Alert variant='destructive' className='mb-4'>
              <AlertCircle className='h-4 w-4' />
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
          {loading ? (
            <div className='space-y-2'>
              <Skeleton className='h-12 w-full' />
              <Skeleton className='h-12 w-full' />
            </div>
          ) : tokens.length === 0 ? (
            <p className='text-muted-foreground text-sm'>
              No tokens yet. Create one above.
            </p>
          ) : (
            <div className='overflow-x-auto'>
              <table className='w-full text-sm'>
                <thead>
                  <tr className='text-muted-foreground border-b text-left'>
                    <th className='py-2 pr-4 font-medium'>Name</th>
                    <th className='py-2 pr-4 font-medium'>Token</th>
                    <th className='py-2 pr-4 font-medium'>Created</th>
                    <th className='py-2 pr-4 font-medium'>Last used</th>
                    <th className='py-2 pr-4 font-medium'>Expires</th>
                    <th className='py-2 font-medium'></th>
                  </tr>
                </thead>
                <tbody>
                  {tokens.map((t) => (
                    <tr key={t.id} className='border-b last:border-0'>
                      <td className='py-2 pr-4'>{t.name}</td>
                      <td className='py-2 pr-4 font-mono text-xs'>
                        {t.token_preview}
                      </td>
                      <td className='text-muted-foreground py-2 pr-4'>
                        {formatTs(t.created_at)}
                      </td>
                      <td className='text-muted-foreground py-2 pr-4'>
                        {formatTs(t.last_used_at)}
                      </td>
                      <td className='text-muted-foreground py-2 pr-4'>
                        {t.expires_at ? formatTs(t.expires_at) : 'Never'}
                      </td>
                      <td className='py-2'>
                        <Button
                          type='button'
                          variant='ghost'
                          size='sm'
                          onClick={() => void handleRevoke(t.id)}
                        >
                          <Trash2 className='h-4 w-4' />
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
