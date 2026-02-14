'use client';

import { type JSX, useCallback, useState } from 'react';
import Image from 'next/image';
import { Copy, Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import QRCode from 'qrcode';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { AdminService } from '@/lib/api/services/admin';

async function generateQRCodeSVG(text: string): Promise<string> {
  try {
    return await QRCode.toDataURL(text, {
      type: 'image/png',
      width: 200,
      margin: 1,
      color: {
        dark: '#000000',
        light: '#FFFFFF',
      },
    });
  } catch (error) {
    console.error('Failed to generate QR code:', error);
    return '';
  }
}

interface SimpleLightningTopupProps {
  providerId: number;
  baseUrl: string;
  onSuccess?: () => void;
}

export function SimpleLightningTopup({
  providerId,
  onSuccess,
}: SimpleLightningTopupProps): JSX.Element {
  const [amount, setAmount] = useState('');
  const [isCreating, setIsCreating] = useState(false);
  const [invoice, setInvoice] = useState<{
    bolt11: string;
    invoice_id: string;
  } | null>(null);
  const [qrCode, setQrCode] = useState<string>('');
  const [isWaiting, setIsWaiting] = useState(false);

  const handleCopy = (text: string) => {
    navigator.clipboard.writeText(text);
    toast.success('Copied to clipboard');
  };

  const pollStatus = useCallback(
    async (invoiceId: string) => {
      const maxAttempts = 60; // 5 minutes with 5 second intervals
      let attempts = 0;

      const poll = async () => {
        try {
          const response = await AdminService.checkTopupStatus(
            providerId,
            invoiceId
          );

          if (response.paid) {
            toast.success('Payment received!');
            setInvoice(null);
            setQrCode('');
            setIsWaiting(false);
            onSuccess?.();
            return;
          }

          attempts++;
          if (attempts < maxAttempts) {
            setTimeout(poll, 5000);
          } else {
            toast.error('Payment timeout - please check manually');
            setIsWaiting(false);
          }
        } catch (e) {
          console.error('Failed to poll topup status:', e);
          attempts++;
          if (attempts < maxAttempts) {
            setTimeout(poll, 5000);
          } else {
            toast.error('Failed to check payment status');
            setIsWaiting(false);
          }
        }
      };
      poll();
    },
    [providerId, onSuccess]
  );

  const handleCreate = async () => {
    const amt = parseInt(amount);
    if (!amt) {
      toast.error('Enter a valid amount');
      return;
    }
    setIsCreating(true);
    try {
      const response = await AdminService.initiateProviderTopup(
        providerId,
        amt
      );
      if (!response.ok || !response.topup_data)
        throw new Error('Failed to create invoice');

      const bolt11 = response.topup_data.payment_request as string;
      setInvoice({
        bolt11,
        invoice_id: response.topup_data.invoice_id as string,
      });

      const qr = await generateQRCodeSVG(bolt11);
      setQrCode(qr);

      setIsWaiting(true);
      pollStatus(response.topup_data.invoice_id as string);
    } catch (e) {
      const error = e as Error;
      toast.error(error.message || 'Failed to request invoice from backend');
    } finally {
      setIsCreating(false);
    }
  };

  return (
    <div className='bg-muted/20 space-y-3 rounded-lg border p-4'>
      <div className='flex gap-2'>
        <Input
          type='number'
          placeholder='Amount in sats'
          value={amount}
          onChange={(e) => setAmount(e.target.value)}
          className='h-9'
        />
        <Button
          onClick={handleCreate}
          disabled={isCreating || isWaiting}
          size='sm'
        >
          {isCreating ? 'Creating...' : 'Get Invoice'}
        </Button>
      </div>

      {invoice && (
        <div className='space-y-2 border-t pt-2'>
          <div className='text-muted-foreground flex items-center justify-between text-xs'>
            <span>Invoice Generated</span>
            <Button
              variant='ghost'
              size='icon'
              className='h-6 w-6'
              onClick={() => handleCopy(invoice.bolt11)}
            >
              <Copy className='h-3 w-3' />
            </Button>
          </div>
          {qrCode && (
            <div className='flex justify-center py-2'>
              <Image
                src={qrCode}
                alt='Lightning Invoice QR Code'
                className='h-40 w-40'
                width={160}
                height={160}
                unoptimized
              />
            </div>
          )}
          <div className='bg-muted rounded border p-2 font-mono text-[10px] break-all'>
            {invoice.bolt11}
          </div>
          {isWaiting && (
            <div className='flex animate-pulse items-center gap-2 text-xs text-orange-600'>
              <Loader2 className='h-3 w-3 animate-spin' />
              Waiting for payment...
            </div>
          )}
        </div>
      )}
    </div>
  );
}
