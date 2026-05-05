'use client';

import { useState } from 'react';
import Image from 'next/image';
import { Copy, Loader2, Zap, KeyRound } from 'lucide-react';
import { toast } from 'sonner';
import QRCode from 'qrcode';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';

interface RoutstrCreateKeySectionProps {
  baseUrl: string;
  onApiKeyCreated: (apiKey: string) => void;
}

async function generateQR(text: string): Promise<string> {
  try {
    return await QRCode.toDataURL(text, {
      type: 'image/png',
      width: 200,
      margin: 1,
      color: { dark: '#000000', light: '#FFFFFF' },
    });
  } catch {
    return '';
  }
}

export function RoutstrCreateKeySection({
  baseUrl,
  onApiKeyCreated,
}: RoutstrCreateKeySectionProps) {
  // Lightning state
  const [lnAmount, setLnAmount] = useState('');
  const [lnInvoice, setLnInvoice] = useState<{
    bolt11: string;
    invoice_id: string;
  } | null>(null);
  const [lnQrCode, setLnQrCode] = useState('');
  const [isCreatingLn, setIsCreatingLn] = useState(false);
  const [isWaitingLn, setIsWaitingLn] = useState(false);

  // Cashu state
  const [cashuToken, setCashuToken] = useState('');
  const [isCreatingCashu, setIsCreatingCashu] = useState(false);

  if (!baseUrl) {
    return (
      <div className='bg-muted/30 rounded-lg border p-4'>
        <p className='text-muted-foreground text-sm'>
          Enter the upstream node Base URL above to enable key creation.
        </p>
      </div>
    );
  }

  const cleanUrl = baseUrl.replace(/\/+$/, '');

  const handleCopy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      toast.success('Copied to clipboard');
    } catch {
      toast.error('Failed to copy');
    }
  };

  const pollInvoiceStatus = (invoiceId: string) => {
    let attempts = 0;
    const maxAttempts = 60;

    const poll = async () => {
      try {
        const resp = await fetch(
          `${cleanUrl}/v1/balance/lightning/invoice/${invoiceId}/status`
        );
        if (!resp.ok) throw new Error('Failed to check status');

        const status = await resp.json();

        if (status.status === 'paid' && status.api_key) {
          onApiKeyCreated(status.api_key);
          setLnInvoice(null);
          setLnQrCode('');
          setIsWaitingLn(false);
          setLnAmount('');
          toast.success('Payment received! API key created.');
          return;
        }

        if (status.status === 'expired' || status.status === 'cancelled') {
          toast.error('Invoice expired or cancelled');
          setIsWaitingLn(false);
          return;
        }

        attempts++;
        if (attempts < maxAttempts) {
          setTimeout(poll, 5000);
        } else {
          toast.error('Payment timeout');
          setIsWaitingLn(false);
        }
      } catch {
        attempts++;
        if (attempts < maxAttempts) {
          setTimeout(poll, 5000);
        } else {
          setIsWaitingLn(false);
        }
      }
    };

    poll();
  };

  const handleCreateLightning = async () => {
    const amount = parseInt(lnAmount);
    if (!amount || amount <= 0) {
      toast.error('Enter a valid amount in sats');
      return;
    }

    setIsCreatingLn(true);
    try {
      const resp = await fetch(`${cleanUrl}/v1/balance/lightning/invoice`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ amount_sats: amount, purpose: 'create' }),
      });

      if (!resp.ok) {
        const errorText = await resp.text();
        throw new Error(errorText || 'Failed to create invoice');
      }

      const data = await resp.json();
      setLnInvoice({ bolt11: data.bolt11, invoice_id: data.invoice_id });

      const qr = await generateQR(data.bolt11);
      setLnQrCode(qr);
      setIsWaitingLn(true);

      pollInvoiceStatus(data.invoice_id);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed to create invoice');
    } finally {
      setIsCreatingLn(false);
    }
  };

  const handleCreateCashu = async () => {
    if (!cashuToken.trim()) {
      toast.error('Paste a Cashu token');
      return;
    }

    setIsCreatingCashu(true);
    try {
      const params = new URLSearchParams({
        initial_balance_token: cashuToken.trim(),
      });
      const resp = await fetch(
        `${cleanUrl}/v1/balance/create?${params.toString()}`,
        { method: 'GET', headers: { 'Content-Type': 'application/json' } }
      );

      if (!resp.ok) {
        const errorText = await resp.text();
        throw new Error(errorText || 'Failed to create API key');
      }

      const data = await resp.json();
      onApiKeyCreated(data.api_key);
      setCashuToken('');
      toast.success('API key created');
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed to create key');
    } finally {
      setIsCreatingCashu(false);
    }
  };

  return (
    <div className='bg-muted/30 space-y-4 rounded-lg border p-4'>
      <div className='flex items-center justify-between'>
        <Label className='text-sm font-semibold'>Create API Key</Label>
        <Badge variant='outline' className='text-[10px]'>
          External Node
        </Badge>
      </div>

      <p className='text-muted-foreground text-xs'>
        Create an API key on the upstream Routstr node by paying with Lightning
        or Cashu.
      </p>

      <Tabs defaultValue='lightning' className='w-full'>
        <TabsList className='grid w-full grid-cols-2'>
          <TabsTrigger value='lightning' className='gap-1 text-xs'>
            <Zap className='h-3 w-3' />
            Lightning
          </TabsTrigger>
          <TabsTrigger value='cashu' className='gap-1 text-xs'>
            <KeyRound className='h-3 w-3' />
            Cashu
          </TabsTrigger>
        </TabsList>

        <TabsContent value='lightning' className='mt-3 space-y-3'>
          <div className='flex gap-2'>
            <Input
              type='number'
              placeholder='Amount in sats'
              value={lnAmount}
              onChange={(e) => setLnAmount(e.target.value)}
              className='h-9'
              disabled={isWaitingLn}
            />
            <Button
              onClick={handleCreateLightning}
              disabled={isCreatingLn || isWaitingLn}
              size='sm'
            >
              {isCreatingLn ? 'Creating...' : 'Get Invoice'}
            </Button>
          </div>

          {lnInvoice && (
            <div className='space-y-2 border-t pt-2'>
              <div className='text-muted-foreground flex items-center justify-between text-xs'>
                <span>Pay this invoice to create your key</span>
                <Button
                  variant='ghost'
                  size='icon'
                  className='h-6 w-6'
                  onClick={() => handleCopy(lnInvoice.bolt11)}
                >
                  <Copy className='h-3 w-3' />
                </Button>
              </div>
              {lnQrCode && (
                <div className='flex justify-center py-2'>
                  <Image
                    src={lnQrCode}
                    alt='Lightning Invoice QR Code'
                    className='h-48 w-48'
                    width={192}
                    height={192}
                    unoptimized
                  />
                </div>
              )}
              <div className='bg-muted rounded border p-2 font-mono text-[10px] break-all'>
                {lnInvoice.bolt11}
              </div>
              {isWaitingLn && (
                <div className='flex animate-pulse items-center gap-2 text-xs text-orange-600'>
                  <Loader2 className='h-3 w-3 animate-spin' />
                  Waiting for payment...
                </div>
              )}
            </div>
          )}
        </TabsContent>

        <TabsContent value='cashu' className='mt-3 space-y-3'>
          <Textarea
            placeholder='Paste Cashu token (cashuA1...)'
            value={cashuToken}
            onChange={(e) => setCashuToken(e.target.value)}
            rows={3}
            className='font-mono text-xs'
          />
          <Button
            onClick={handleCreateCashu}
            disabled={isCreatingCashu}
            size='sm'
            className='w-full'
          >
            {isCreatingCashu ? 'Creating...' : 'Create API Key'}
          </Button>
          <p className='text-muted-foreground text-[10px]'>
            Redeems the token instantly and returns an API key.
          </p>
        </TabsContent>
      </Tabs>
    </div>
  );
}
