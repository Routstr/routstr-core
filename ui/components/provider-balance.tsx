'use client';

import Image from 'next/image';
import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CheckCircle2 } from 'lucide-react';
import { toast } from 'sonner';
import { AdminService } from '@/lib/api/services/admin';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';

interface ProviderBalanceProps {
  providerId: number;
  platformUrl?: string | null;
}

export function ProviderBalance({
  providerId,
  platformUrl,
}: ProviderBalanceProps) {
  const [isTopupDialogOpen, setIsTopupDialogOpen] = useState(false);
  const [topupAmount, setTopupAmount] = useState('');
  const [topupError, setTopupError] = useState('');
  const [isHovered, setIsHovered] = useState(false);
  const [invoiceData, setInvoiceData] = useState<{
    payment_request: string;
    invoice_id: string;
  } | null>(null);
  const [paymentStatus, setPaymentStatus] = useState<'pending' | 'paid' | null>(
    null
  );
  const queryClient = useQueryClient();

  const {
    data: balanceData,
    isLoading,
    error,
  } = useQuery({
    queryKey: ['provider-balance', providerId],
    queryFn: () => AdminService.getProviderBalance(providerId),
    refetchInterval: 30000,
    refetchOnWindowFocus: true,
    retry: 1,
  });

  const { data: statusData } = useQuery({
    queryKey: ['topup-status', providerId, invoiceData?.invoice_id],
    queryFn: () =>
      AdminService.checkTopupStatus(providerId, invoiceData!.invoice_id),
    enabled: !!invoiceData && paymentStatus === 'pending',
    refetchInterval: 2000,
  });

  useEffect(() => {
    if (statusData?.paid === true) {
      setPaymentStatus('paid');
      queryClient.invalidateQueries({
        queryKey: ['provider-balance', providerId],
      });
      toast.success('Payment received!', {
        description: 'Your balance has been updated.',
      });
    }
  }, [statusData, queryClient, providerId]);

  const topupMutation = useMutation({
    mutationFn: async (amount: number) => {
      const result = await AdminService.initiateProviderTopup(
        providerId,
        amount
      );
      return result;
    },
    onSuccess: (data) => {
      if (data?.topup_data?.payment_request && data?.topup_data?.invoice_id) {
        setInvoiceData({
          payment_request: data.topup_data.payment_request as string,
          invoice_id: data.topup_data.invoice_id as string,
        });
        setPaymentStatus('pending');
      } else {
        toast.error('No invoice returned from provider');
        setIsTopupDialogOpen(false);
      }
    },
    onError: (error: Error) => {
      toast.error(`Failed to initiate top-up: ${error.message}`);
    },
  });

  const handleTopup = () => {
    const amount = parseFloat(topupAmount);

    if (isNaN(amount)) {
      setTopupError('Please enter a valid amount');
      return;
    }

    if (amount < 1 || amount > 500) {
      setTopupError('Amount must be between $1 and $500');
      return;
    }

    topupMutation.mutate(amount);
  };

  const handleTopUpClick = () => {
    if (
      platformUrl &&
      (platformUrl.includes('openrouter.ai') ||
        platformUrl.includes('openai.com'))
    ) {
      window.open(platformUrl, '_blank');
      return;
    }

    setIsTopupDialogOpen(true);
  };

  const handleCloseDialog = () => {
    setIsTopupDialogOpen(false);
    setTopupAmount('');
    setTopupError('');
    setInvoiceData(null);
    setPaymentStatus(null);
  };

  if (isLoading) {
    return <Skeleton className='h-9 w-24' />;
  }

  if (
    error ||
    !balanceData?.ok ||
    balanceData.balance_data === undefined ||
    balanceData.balance_data === null
  ) {
    return null;
  }

  const balance = balanceData.balance_data;
  let displayValue = 'N/A';

  if (typeof balance === 'number') {
    displayValue = `$${balance.toFixed(2)}`;
  } else if (balance && typeof balance === 'object') {
    const b = balance as Record<string, unknown>;
    if (typeof b.balance === 'number') {
      displayValue = `$${b.balance.toFixed(2)}`;
    } else if (typeof b.balance === 'string') {
      displayValue = b.balance;
    } else if (b.amount !== undefined) {
      displayValue = `$${Number(b.amount).toFixed(2)}`;
    }
  }

  return (
    <>
      <Button
        variant='outline'
        size='sm'
        onClick={handleTopUpClick}
        onMouseEnter={() => setIsHovered(true)}
        onMouseLeave={() => setIsHovered(false)}
        className='w-full font-mono sm:w-auto'
      >
        {isHovered ? 'Top Up' : displayValue}
      </Button>

      <Dialog open={isTopupDialogOpen} onOpenChange={handleCloseDialog}>
        <DialogContent className='sm:max-w-md'>
          <DialogHeader>
            <DialogTitle>
              {paymentStatus === 'paid'
                ? 'Payment Confirmed!'
                : 'Top Up Balance'}
            </DialogTitle>
            <DialogDescription>
              {paymentStatus === 'paid'
                ? 'Your account balance has been updated.'
                : invoiceData
                  ? 'Scan the QR code or copy the Lightning invoice to pay.'
                  : 'Enter the amount you want to add to your account balance.'}
            </DialogDescription>
          </DialogHeader>

          {paymentStatus === 'paid' ? (
            <div className='flex flex-col items-center gap-4 py-6'>
              <Badge className='gap-1.5 px-3 py-1'>
                <CheckCircle2 className='h-4 w-4' />
                Top-up Successful
              </Badge>
              <p className='text-muted-foreground text-center text-sm'>
                Your provider balance has been updated.
              </p>
            </div>
          ) : invoiceData ? (
            <div className='flex flex-col items-center gap-4 py-4'>
              <div className='rounded-lg border p-2'>
                <Image
                  src={`https://api.qrserver.com/v1/create-qr-code/?size=256x256&data=${encodeURIComponent(
                    invoiceData.payment_request
                  )}`}
                  alt='Lightning invoice QR code'
                  width={256}
                  height={256}
                  className='h-56 w-56 sm:h-64 sm:w-64'
                  unoptimized
                />
              </div>
              <div className='w-full space-y-2'>
                <Label htmlFor='invoice'>Lightning Invoice</Label>
                <div className='flex flex-col gap-2 sm:flex-row'>
                  <Input
                    id='invoice'
                    value={invoiceData.payment_request}
                    readOnly
                    className='font-mono text-xs'
                  />
                  <Button
                    size='sm'
                    variant='outline'
                    onClick={() => {
                      navigator.clipboard.writeText(
                        invoiceData.payment_request
                      );
                      toast.success('Invoice copied to clipboard!');
                    }}
                    className='w-full sm:w-auto'
                  >
                    Copy
                  </Button>
                </div>
              </div>
              {paymentStatus === 'pending' && (
                <p className='text-muted-foreground text-center text-sm'>
                  Waiting for payment...
                </p>
              )}
            </div>
          ) : (
            <div className='grid gap-4 py-4'>
              <div className='grid gap-2'>
                <Label htmlFor='topup_amount'>Amount (USD)</Label>
                <Input
                  id='topup_amount'
                  type='number'
                  placeholder='Enter amount (1-500)'
                  value={topupAmount}
                  onChange={(e) => {
                    setTopupAmount(e.target.value);
                    setTopupError('');
                  }}
                  min='1'
                  max='500'
                  step='0.01'
                />
                {topupError && (
                  <p className='text-destructive text-sm'>{topupError}</p>
                )}
              </div>
            </div>
          )}

          <DialogFooter>
            {paymentStatus === 'paid' ? (
              <Button onClick={handleCloseDialog} className='w-full'>
                Done
              </Button>
            ) : invoiceData ? (
              <Button
                variant='outline'
                onClick={handleCloseDialog}
                className='w-full'
              >
                Cancel
              </Button>
            ) : (
              <>
                <Button
                  variant='outline'
                  onClick={handleCloseDialog}
                  className='w-full sm:w-auto'
                >
                  Cancel
                </Button>
                <Button
                  onClick={handleTopup}
                  disabled={topupMutation.isPending || !topupAmount}
                  className='w-full sm:w-auto'
                >
                  {topupMutation.isPending
                    ? 'Processing...'
                    : 'Generate Invoice'}
                </Button>
              </>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
