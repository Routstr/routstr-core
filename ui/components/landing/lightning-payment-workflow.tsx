'use client';

import { type JSX, useCallback, useState } from 'react';
import Image from 'next/image';
import { Copy, Zap, CheckCircle, Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import QRCode from 'qrcode';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Separator } from '@/components/ui/separator';

type WalletSnapshot = {
  apiKey: string;
  balanceMsats: number;
  reservedMsats: number;
};

type LightningInvoice = {
  invoice_id: string;
  bolt11: string;
  amount_sats: number;
  expires_at: number;
  payment_hash: string;
};

type InvoiceStatus = {
  status: string;
  api_key?: string;
  amount_sats: number;
  paid_at?: number;
  created_at: number;
  expires_at: number;
};

interface LightningPaymentWorkflowProps {
  baseUrl: string;
  onApiKeyCreated?: (apiKey: string, walletInfo: WalletSnapshot) => void;
}

function formatSats(msats: number): string {
  return new Intl.NumberFormat('en-US').format(Math.floor(msats / 1000));
}

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

export function LightningPaymentWorkflow({
  baseUrl,
  onApiKeyCreated,
}: LightningPaymentWorkflowProps): JSX.Element {
  const [createAmount, setCreateAmount] = useState<string>('');
  const [topupAmount, setTopupAmount] = useState<string>('');
  const [topupApiKey, setTopupApiKey] = useState<string>('');
  const [recoverInvoice, setRecoverInvoice] = useState<string>('');

  const [createInvoice, setCreateInvoice] = useState<LightningInvoice | null>(
    null
  );
  const [topupInvoice, setTopupInvoice] = useState<LightningInvoice | null>(
    null
  );

  const [createQRCode, setCreateQRCode] = useState<string>('');
  const [topupQRCode, setTopupQRCode] = useState<string>('');

  const [createdApiKey, setCreatedApiKey] = useState<string>('');
  const [topupApiKeyResult, setTopupApiKeyResult] = useState<string>('');
  const [recoveredApiKey, setRecoveredApiKey] = useState<string>('');
  const [isWaitingPayment, setIsWaitingPayment] = useState(false);
  const [isWaitingTopupPayment, setIsWaitingTopupPayment] = useState(false);

  const [isCreating, setIsCreating] = useState(false);
  const [isTopupping, setIsTopupping] = useState(false);
  const [isRecovering, setIsRecovering] = useState(false);

  const [hasInteractedCreate, setHasInteractedCreate] = useState(false);
  const [hasInteractedTopup, setHasInteractedTopup] = useState(false);
  const [hasInteractedRecover, setHasInteractedRecover] = useState(false);

  const handleCopy = useCallback(async (value: string): Promise<void> => {
    if (!value) return;

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

  const pollInvoiceStatus = useCallback(
    async (invoiceId: string, onPaid: (status: InvoiceStatus) => void) => {
      let attempts = 0;
      const maxAttempts = 60; // 5 minutes with 5 second intervals

      const poll = async () => {
        try {
          const response = await fetch(
            `${baseUrl}/v1/balance/lightning/invoice/${invoiceId}/status`
          );
          if (!response.ok) {
            throw new Error('Failed to check invoice status');
          }

          const status: InvoiceStatus = await response.json();

          if (status.status === 'paid' && status.api_key) {
            onPaid(status);
            return;
          }

          if (status.status === 'expired' || status.status === 'cancelled') {
            toast.error('Invoice expired or cancelled');
            return;
          }

          attempts++;
          if (attempts < maxAttempts) {
            setTimeout(poll, 5000); // Poll every 5 seconds
          } else {
            toast.error('Payment timeout - please check manually');
          }
        } catch (error) {
          console.error('Failed to poll invoice status:', error);
          attempts++;
          if (attempts < maxAttempts) {
            setTimeout(poll, 5000);
          }
        }
      };

      poll();
    },
    [baseUrl]
  );

  const handleCreateInvoice = useCallback(async (): Promise<void> => {
    const amount = parseInt(createAmount);
    if (!amount || amount <= 0) {
      toast.error('Please enter a valid amount');
      return;
    }

    setIsCreating(true);

    try {
      const response = await fetch(`${baseUrl}/v1/balance/lightning/invoice`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          amount_sats: amount,
          purpose: 'create',
        }),
      });

      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(errorText || 'Failed to create invoice');
      }

      const invoice: LightningInvoice = await response.json();
      setCreateInvoice(invoice);

      const qrCode = await generateQRCodeSVG(invoice.bolt11);
      setCreateQRCode(qrCode);
      setIsWaitingPayment(true);

      toast.success('Lightning invoice created - waiting for payment...');

      pollInvoiceStatus(invoice.invoice_id, (status) => {
        if (status.api_key) {
          const walletInfo: WalletSnapshot = {
            apiKey: status.api_key,
            balanceMsats: status.amount_sats * 1000,
            reservedMsats: 0,
          };
          onApiKeyCreated?.(status.api_key, walletInfo);
          setCreatedApiKey(status.api_key);
          setIsWaitingPayment(false);
          toast.success('Payment received! API key created.');
          setCreateInvoice(null);
          setCreateAmount('');
          setCreateQRCode('');
        }
      });
    } catch (error) {
      console.error(error);
      toast.error(
        error instanceof Error ? error.message : 'Failed to create invoice'
      );
      setIsWaitingPayment(false);
    } finally {
      setIsCreating(false);
    }
  }, [createAmount, baseUrl, pollInvoiceStatus, onApiKeyCreated]);

  const handleTopupInvoice = useCallback(async (): Promise<void> => {
    const amount = parseInt(topupAmount);
    if (!amount || amount <= 0) {
      toast.error('Please enter a valid amount');
      return;
    }

    if (!topupApiKey.trim()) {
      toast.error('Please enter your API key');
      return;
    }

    setIsTopupping(true);

    try {
      const response = await fetch(`${baseUrl}/v1/balance/lightning/invoice`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          amount_sats: amount,
          purpose: 'topup',
          api_key: topupApiKey.trim(),
        }),
      });

      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(errorText || 'Failed to create topup invoice');
      }

      const invoice: LightningInvoice = await response.json();
      setTopupInvoice(invoice);

      const qrCode = await generateQRCodeSVG(invoice.bolt11);
      setTopupQRCode(qrCode);
      setIsWaitingTopupPayment(true);

      toast.success('Lightning topup invoice created - waiting for payment...');

      pollInvoiceStatus(invoice.invoice_id, (status) => {
        if (status.api_key) {
          const walletInfo: WalletSnapshot = {
            apiKey: status.api_key,
            balanceMsats: status.amount_sats * 1000,
            reservedMsats: 0,
          };
          onApiKeyCreated?.(status.api_key, walletInfo);
          setTopupApiKeyResult(status.api_key);
          setIsWaitingTopupPayment(false);
          toast.success(
            `Payment received! Added ${formatSats(status.amount_sats * 1000)} sats.`
          );
          setTopupInvoice(null);
          setTopupAmount('');
          setTopupQRCode('');
        }
      });
    } catch (error) {
      console.error(error);
      toast.error(
        error instanceof Error
          ? error.message
          : 'Failed to create topup invoice'
      );
      setIsWaitingTopupPayment(false);
    } finally {
      setIsTopupping(false);
    }
  }, [topupAmount, topupApiKey, baseUrl, pollInvoiceStatus, onApiKeyCreated]);

  const handleRecoverInvoice = useCallback(async (): Promise<void> => {
    if (!recoverInvoice.trim()) {
      toast.error('Please enter a BOLT11 invoice');
      return;
    }

    setIsRecovering(true);

    try {
      const response = await fetch(`${baseUrl}/v1/balance/lightning/recover`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          bolt11: recoverInvoice.trim(),
        }),
      });

      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(errorText || 'Invoice not found or not paid');
      }

      const status: InvoiceStatus = await response.json();

      if (status.status === 'paid' && status.api_key) {
        const walletInfo: WalletSnapshot = {
          apiKey: status.api_key,
          balanceMsats: status.amount_sats * 1000,
          reservedMsats: 0,
        };
        onApiKeyCreated?.(status.api_key, walletInfo);
        setRecoveredApiKey(status.api_key);
        toast.success('API key recovered successfully!');
        setRecoverInvoice('');
      } else {
        toast.error(`Invoice status: ${status.status}`);
      }
    } catch (error) {
      console.error(error);
      toast.error(
        error instanceof Error ? error.message : 'Failed to recover invoice'
      );
    } finally {
      setIsRecovering(false);
    }
  }, [recoverInvoice, baseUrl, onApiKeyCreated]);

  const showCreateDetails =
    hasInteractedCreate || createAmount.trim().length > 0;
  const showTopupDetails =
    hasInteractedTopup ||
    topupAmount.trim().length > 0 ||
    topupApiKey.trim().length > 0;
  const showRecoverDetails =
    hasInteractedRecover || recoverInvoice.trim().length > 0;

  return (
    <Card>
      <CardHeader className='space-y-1'>
        <CardTitle className='flex items-center gap-2 text-xl'>
          <Zap className='text-primary h-5 w-5' />
          Lightning Payment Workflow
        </CardTitle>
        <p className='text-muted-foreground text-xs tracking-wide uppercase'>
          Create and manage API keys using Lightning Network payments
        </p>
      </CardHeader>
      <CardContent className='space-y-6'>
        <section className='space-y-2'>
          <header className='text-muted-foreground flex items-center justify-between text-[0.7rem] tracking-wider uppercase'>
            <span>1 · Create key with Lightning</span>
            {showCreateDetails && (
              <span className='text-primary'>Amount specified</span>
            )}
          </header>
          <Input
            type='number'
            value={createAmount}
            onChange={(event) => setCreateAmount(event.target.value)}
            placeholder='Amount in sats (e.g., 1000)'
            className='text-sm'
            onFocus={() => setHasInteractedCreate(true)}
          />
          {showCreateDetails && (
            <div className='space-y-3'>
              <Button
                onClick={handleCreateInvoice}
                disabled={isCreating || !!createInvoice}
                className='gap-2'
              >
                {isCreating
                  ? 'Creating invoice...'
                  : 'Create Lightning Invoice'}
              </Button>

              {createInvoice && (
                <div className='bg-muted/30 space-y-3 rounded-lg border p-4'>
                  <div className='text-muted-foreground flex items-center justify-between text-[0.7rem] tracking-wider uppercase'>
                    <span>
                      Lightning Invoice ({createInvoice.amount_sats} sats)
                    </span>
                    <Button
                      variant='outline'
                      size='sm'
                      className='gap-1 text-xs'
                      onClick={() => handleCopy(createInvoice.bolt11)}
                    >
                      <Copy className='h-3 w-3' />
                      Copy
                    </Button>
                  </div>

                  {isWaitingPayment && (
                    <div className='flex items-center justify-center gap-2 rounded-lg border border-orange-200 bg-orange-50 p-3 dark:border-orange-800 dark:bg-orange-950'>
                      <Loader2 className='h-4 w-4 animate-spin text-orange-600' />
                      <span className='text-sm font-medium text-orange-800 dark:text-orange-200'>
                        Waiting for payment...
                      </span>
                    </div>
                  )}

                  {createQRCode && (
                    <div className='flex justify-center'>
                      <Image
                        src={createQRCode}
                        alt='QR Code'
                        className='h-48 w-48'
                        width={192}
                        height={192}
                        unoptimized
                      />
                    </div>
                  )}

                  <Textarea
                    value={createInvoice.bolt11}
                    readOnly
                    rows={4}
                    className='font-mono text-xs'
                  />

                  <p className='text-muted-foreground text-center text-xs'>
                    Scan QR code or copy invoice. Payment will be detected
                    automatically.
                  </p>
                </div>
              )}

              {createdApiKey && (
                <div className='space-y-3 rounded-lg border border-green-200 bg-green-50 p-4 dark:border-green-800 dark:bg-green-950'>
                  <div className='flex items-center justify-between text-[0.7rem] tracking-wider text-green-700 uppercase dark:text-green-300'>
                    <div className='flex items-center gap-2'>
                      <CheckCircle className='h-4 w-4' />
                      <span>API Key Created Successfully</span>
                    </div>
                    <Button
                      variant='outline'
                      size='sm'
                      className='gap-1 border-green-300 text-xs hover:bg-green-100 dark:border-green-700 dark:hover:bg-green-900'
                      onClick={() => handleCopy(createdApiKey)}
                    >
                      <Copy className='h-3 w-3' />
                      Copy
                    </Button>
                  </div>

                  <Textarea
                    value={createdApiKey}
                    readOnly
                    rows={2}
                    className='border-green-200 bg-white font-mono text-xs dark:border-green-800 dark:bg-green-950/50'
                  />

                  <div className='flex items-center justify-between text-xs text-green-700 dark:text-green-300'>
                    <span>Your API key is ready to use!</span>
                    <Button
                      variant='ghost'
                      size='sm'
                      className='h-6 px-2 text-xs text-green-700 hover:text-green-800 dark:text-green-300 dark:hover:text-green-200'
                      onClick={() => setCreatedApiKey('')}
                    >
                      Dismiss
                    </Button>
                  </div>
                </div>
              )}
            </div>
          )}
        </section>

        <Separator />

        <section className='space-y-2'>
          <header className='text-muted-foreground flex items-center justify-between text-[0.7rem] tracking-wider uppercase'>
            <span>2 · Top up existing key</span>
            {showTopupDetails && (
              <span className='text-primary'>Ready to create invoice</span>
            )}
          </header>
          <div className='space-y-2'>
            <Input
              value={topupApiKey}
              onChange={(event) => setTopupApiKey(event.target.value)}
              placeholder='Your API key (sk-...)'
              className='font-mono text-sm'
              onFocus={() => setHasInteractedTopup(true)}
            />
            <Input
              type='number'
              value={topupAmount}
              onChange={(event) => setTopupAmount(event.target.value)}
              placeholder='Amount to add in sats'
              className='text-sm'
              onFocus={() => setHasInteractedTopup(true)}
            />
          </div>

          {showTopupDetails && (
            <div className='space-y-3'>
              <Button
                onClick={handleTopupInvoice}
                disabled={isTopupping || !!topupInvoice}
                variant='outline'
                className='gap-2'
              >
                {isTopupping ? 'Creating invoice...' : 'Create Topup Invoice'}
              </Button>

              {topupInvoice && (
                <div className='bg-muted/30 space-y-3 rounded-lg border p-4'>
                  <div className='text-muted-foreground flex items-center justify-between text-[0.7rem] tracking-wider uppercase'>
                    <span>Topup Invoice ({topupInvoice.amount_sats} sats)</span>
                    <Button
                      variant='outline'
                      size='sm'
                      className='gap-1 text-xs'
                      onClick={() => handleCopy(topupInvoice.bolt11)}
                    >
                      <Copy className='h-3 w-3' />
                      Copy
                    </Button>
                  </div>

                  {topupQRCode && (
                    <div className='flex justify-center'>
                      <Image
                        src={topupQRCode}
                        alt='QR Code'
                        className='h-48 w-48'
                        width={192}
                        height={192}
                        unoptimized
                      />
                    </div>
                  )}

                  <Textarea
                    value={topupInvoice.bolt11}
                    readOnly
                    rows={4}
                    className='font-mono text-xs'
                  />

                  <p className='text-muted-foreground text-center text-xs'>
                    Scan QR code or copy invoice. Balance will be added
                    automatically.
                  </p>

                  {isWaitingTopupPayment && (
                    <div className='flex items-center justify-center gap-2 rounded-lg border border-orange-200 bg-orange-50 p-3 dark:border-orange-800 dark:bg-orange-950'>
                      <Loader2 className='h-4 w-4 animate-spin text-orange-600' />
                      <span className='text-sm font-medium text-orange-800 dark:text-orange-200'>
                        Waiting for payment...
                      </span>
                    </div>
                  )}
                </div>
              )}

              {topupApiKeyResult && (
                <div className='space-y-3 rounded-lg border border-blue-200 bg-blue-50 p-4 dark:border-blue-800 dark:bg-blue-950'>
                  <div className='flex items-center justify-between text-[0.7rem] tracking-wider text-blue-700 uppercase dark:text-blue-300'>
                    <div className='flex items-center gap-2'>
                      <CheckCircle className='h-4 w-4' />
                      <span>Topup Successful</span>
                    </div>
                    <Button
                      variant='outline'
                      size='sm'
                      className='gap-1 border-blue-300 text-xs hover:bg-blue-100 dark:border-blue-700 dark:hover:bg-blue-900'
                      onClick={() => handleCopy(topupApiKeyResult)}
                    >
                      <Copy className='h-3 w-3' />
                      Copy
                    </Button>
                  </div>

                  <Textarea
                    value={topupApiKeyResult}
                    readOnly
                    rows={2}
                    className='border-blue-200 bg-white font-mono text-xs dark:border-blue-800 dark:bg-blue-950/50'
                  />

                  <div className='flex items-center justify-between text-xs text-blue-700 dark:text-blue-300'>
                    <span>Balance has been added to your API key!</span>
                    <Button
                      variant='ghost'
                      size='sm'
                      className='h-6 px-2 text-xs text-blue-700 hover:text-blue-800 dark:text-blue-300 dark:hover:text-blue-200'
                      onClick={() => setTopupApiKeyResult('')}
                    >
                      Dismiss
                    </Button>
                  </div>
                </div>
              )}
            </div>
          )}
        </section>

        <Separator />

        <section className='space-y-2'>
          <header className='text-muted-foreground flex items-center justify-between text-[0.7rem] tracking-wider uppercase'>
            <span>3 · Recover from invoice</span>
            {showRecoverDetails && (
              <span className='text-primary'>Invoice provided</span>
            )}
          </header>
          <Textarea
            value={recoverInvoice}
            onChange={(event) => setRecoverInvoice(event.target.value)}
            placeholder='Paste BOLT11 invoice to recover API key...'
            rows={showRecoverDetails ? 3 : 1}
            className='font-mono text-sm transition-all duration-200'
            onFocus={() => setHasInteractedRecover(true)}
          />
          {showRecoverDetails && (
            <div className='flex flex-wrap gap-2'>
              <Button
                onClick={handleRecoverInvoice}
                disabled={isRecovering}
                variant='secondary'
                className='gap-2'
              >
                {isRecovering ? 'Recovering...' : 'Recover API Key'}
              </Button>
              <span className='text-muted-foreground text-xs'>
                Recovers API key from a paid Lightning invoice.
              </span>
            </div>
          )}

          {recoveredApiKey && (
            <div className='space-y-3 rounded-lg border border-purple-200 bg-purple-50 p-4 dark:border-purple-800 dark:bg-purple-950'>
              <div className='flex items-center justify-between text-[0.7rem] tracking-wider text-purple-700 uppercase dark:text-purple-300'>
                <div className='flex items-center gap-2'>
                  <CheckCircle className='h-4 w-4' />
                  <span>API Key Recovered</span>
                </div>
                <Button
                  variant='outline'
                  size='sm'
                  className='gap-1 border-purple-300 text-xs hover:bg-purple-100 dark:border-purple-700 dark:hover:bg-purple-900'
                  onClick={() => handleCopy(recoveredApiKey)}
                >
                  <Copy className='h-3 w-3' />
                  Copy
                </Button>
              </div>

              <Textarea
                value={recoveredApiKey}
                readOnly
                rows={2}
                className='border-purple-200 bg-white font-mono text-xs dark:border-purple-800 dark:bg-purple-950/50'
              />

              <div className='flex items-center justify-between text-xs text-purple-700 dark:text-purple-300'>
                <span>Your recovered API key is ready to use!</span>
                <Button
                  variant='ghost'
                  size='sm'
                  className='h-6 px-2 text-xs text-purple-700 hover:text-purple-800 dark:text-purple-300 dark:hover:text-purple-200'
                  onClick={() => setRecoveredApiKey('')}
                >
                  Dismiss
                </Button>
              </div>
            </div>
          )}
        </section>
      </CardContent>
    </Card>
  );
}
