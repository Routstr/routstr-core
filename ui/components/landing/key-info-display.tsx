'use client';

import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
} from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Copy, RotateCcw } from 'lucide-react';
import type { WalletSnapshot } from './key-info-details';
import { toast } from 'sonner';

interface KeyInfoDisplayProps {
  walletInfo: WalletSnapshot;
  onResetSpent?: (childKey: string) => Promise<void>;
  isResetting?: string | null;
}

const formatSats = (msats: number) =>
  new Intl.NumberFormat('en-US').format(Math.floor(msats / 1000));
const formatMsats = (msats: number) =>
  new Intl.NumberFormat('en-US').format(msats);
const formatDate = (timestamp: number | null) =>
  timestamp ? new Date(timestamp * 1000).toLocaleDateString() : 'Never';

export function KeyInfoDisplay({
  walletInfo,
  onResetSpent,
  isResetting,
}: KeyInfoDisplayProps) {
  const handleCopy = (value: string) => {
    navigator.clipboard.writeText(value);
    toast.success('Copied to clipboard');
  };

  return (
    <div className='space-y-4'>
      <div className='grid gap-4 md:grid-cols-2'>
        <Card>
          <CardHeader className='pb-2'>
            <CardTitle className='text-lg'>Status & Identity</CardTitle>
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
                <span className='text-muted-foreground text-xs tracking-wider'>
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
              <span className='text-muted-foreground text-sm'>Validity</span>
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
              <span className='text-muted-foreground text-sm'>Total Spent</span>
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
              <CardTitle className='text-lg'>
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
                        {onResetSpent && (
                          <Button
                            variant='ghost'
                            size='icon'
                            className='text-destructive h-8 w-8'
                            title='Reset consumption'
                            disabled={isResetting === ck.api_key}
                            onClick={() => onResetSpent(ck.api_key)}
                          >
                            {isResetting === ck.api_key ? (
                              <RotateCcw className='h-4 w-4 animate-spin' />
                            ) : (
                              <RotateCcw className='h-4 w-4' />
                            )}
                          </Button>
                        )}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>
        )}
    </div>
  );
}
