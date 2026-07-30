'use client';

import { useState, useEffect } from 'react';
import { useMutation } from '@tanstack/react-query';
import { WalletService, BalanceDetail } from '@/lib/api/services/wallet';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Alert, AlertDescription } from '@/components/ui/alert';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { AlertCircle, Copy, CheckCircle, Download } from 'lucide-react';
import { useCopyToClipboard } from '@/hooks/use-copy-to-clipboard';
import { getApiErrorMessage } from '@/lib/api/errors';

interface WithdrawModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  balances: BalanceDetail[];
  onSuccess?: () => void;
}

export function WithdrawModal({
  open,
  onOpenChange,
  balances,
  onSuccess,
}: WithdrawModalProps) {
  const [selectedMintUnit, setSelectedMintUnit] = useState('');
  const [amount, setAmount] = useState('');
  const [withdrawnToken, setWithdrawnToken] = useState('');
  const [confirmedSaved, setConfirmedSaved] = useState(false);
  const { copied: copiedToken, copy } = useCopyToClipboard();

  const availableBalances = balances.filter(
    (b) => !b.error && b.wallet_balance > 0
  );

  const selectedBalance = availableBalances.find(
    (b) => `${b.mint_url}|${b.unit}` === selectedMintUnit
  );

  const withdrawMutation = useMutation({
    mutationFn: async () => {
      if (!selectedBalance) throw new Error('No balance selected');
      const amountNum = parseInt(amount);
      if (isNaN(amountNum) || amountNum <= 0) {
        throw new Error('Invalid amount');
      }
      return WalletService.withdraw(
        amountNum,
        selectedBalance.mint_url,
        selectedBalance.unit
      );
    },
    onSuccess: (data) => {
      setWithdrawnToken(data.token);
      onSuccess?.();
    },
  });

  useEffect(() => {
    if (selectedBalance) {
      const recommendedAmount =
        selectedBalance.owner_balance > 0 ? selectedBalance.owner_balance : 0;
      setAmount(recommendedAmount.toString());
    }
  }, [selectedBalance]);

  useEffect(() => {
    if (!open) {
      setWithdrawnToken('');
      setAmount('');
      setSelectedMintUnit('');
      setConfirmedSaved(false);
    }
  }, [open]);

  // The token is a bearer instrument. Escape, backdrop, X and drag-dismiss all
  // route through here; none of them may destroy it before the user confirms.
  const handleOpenChange = (next: boolean) => {
    if (!next && withdrawnToken && !confirmedSaved) return;
    onOpenChange(next);
  };

  const handleDownloadToken = () => {
    const blob = new Blob([withdrawnToken], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `cashu-token-${Date.now()}.txt`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const amountNum = parseInt(amount) || 0;
  const showWarning =
    selectedBalance &&
    amountNum > selectedBalance.owner_balance &&
    amountNum <= selectedBalance.wallet_balance;

  if (withdrawnToken) {
    return (
      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent className='sm:max-w-xl'>
          <DialogHeader>
            <DialogTitle>Withdrawal complete</DialogTitle>
            <DialogDescription>
              This token is the money. Anyone holding it can redeem it, so save
              it before closing.
            </DialogDescription>
          </DialogHeader>

          <div className='space-y-4'>
            <Textarea
              readOnly
              value={withdrawnToken}
              onFocus={(e) => e.target.select()}
              className='font-mono text-xs leading-relaxed'
              rows={6}
            />

            <div className='flex flex-col gap-2 sm:flex-row'>
              <Button
                onClick={() => copy(withdrawnToken)}
                className='w-full flex-1'
                variant={copiedToken ? 'outline' : 'default'}
              >
                {copiedToken ? (
                  <>
                    <CheckCircle className='mr-2 h-4 w-4' />
                    Copied!
                  </>
                ) : (
                  <>
                    <Copy className='mr-2 h-4 w-4' />
                    Copy Token
                  </>
                )}
              </Button>
              <Button
                onClick={handleDownloadToken}
                variant='outline'
                className='w-full flex-1'
              >
                <Download className='mr-2 h-4 w-4' />
                Download .txt
              </Button>
            </div>

            <p className='text-muted-foreground text-xs'>
              A copy is also kept under Transactions, where you can copy it
              again later.
            </p>

            <div className='flex items-center gap-2'>
              <Checkbox
                id='token-saved'
                checked={confirmedSaved}
                onCheckedChange={(checked) =>
                  setConfirmedSaved(checked === true)
                }
              />
              <Label htmlFor='token-saved' className='text-sm font-normal'>
                I have saved this token
              </Label>
            </div>

            <Button
              onClick={() => handleOpenChange(false)}
              variant='outline'
              disabled={!confirmedSaved}
              className='w-full'
            >
              Close
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    );
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className='sm:max-w-md'>
        <DialogHeader>
          <DialogTitle>Withdraw Balance</DialogTitle>
          <DialogDescription>
            Select mint and currency, then enter the amount to withdraw.
          </DialogDescription>
        </DialogHeader>

        <div className='space-y-4'>
          <div className='space-y-2'>
            <Label htmlFor='mint-select'>Mint / Currency</Label>
            <Select
              value={selectedMintUnit}
              onValueChange={setSelectedMintUnit}
            >
              <SelectTrigger id='mint-select'>
                <SelectValue placeholder='Select mint and currency' />
              </SelectTrigger>
              <SelectContent>
                {availableBalances.map((balance) => (
                  <SelectItem
                    key={`${balance.mint_url}|${balance.unit}`}
                    value={`${balance.mint_url}|${balance.unit}`}
                  >
                    {balance.mint_url
                      .replace('https://', '')
                      .replace('http://', '')}{' '}
                    • {balance.unit.toUpperCase()} ({balance.owner_balance})
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className='space-y-2'>
            <Label htmlFor='amount'>Amount</Label>
            <Input
              id='amount'
              type='number'
              min='1'
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              placeholder='Enter amount'
            />
            {selectedBalance && (
              <div className='space-y-1 text-xs'>
                <p className='text-muted-foreground'>
                  Maximum:{' '}
                  <span className='font-semibold'>
                    {selectedBalance.wallet_balance} {selectedBalance.unit}
                  </span>
                </p>
                <p className='text-muted-foreground'>
                  Your recommended balance:{' '}
                  <span className='font-semibold'>
                    {selectedBalance.owner_balance} {selectedBalance.unit}
                  </span>
                </p>
              </div>
            )}
          </div>

          {showWarning && (
            <Alert variant='destructive'>
              <AlertCircle className='h-5 w-5' />
              <AlertDescription>
                Warning: Withdrawing more than your balance will use user funds.
              </AlertDescription>
            </Alert>
          )}

          {withdrawMutation.isError && (
            <Alert variant='destructive'>
              <AlertCircle className='h-5 w-5' />
              <AlertDescription>
                {getApiErrorMessage(
                  withdrawMutation.error,
                  'Failed to withdraw'
                )}
              </AlertDescription>
            </Alert>
          )}

          <div className='flex flex-col gap-2 sm:flex-row'>
            <Button
              onClick={() => withdrawMutation.mutate()}
              disabled={
                !selectedMintUnit ||
                !amount ||
                withdrawMutation.isPending ||
                amountNum <= 0 ||
                (selectedBalance && amountNum > selectedBalance.wallet_balance)
              }
              className='flex-1'
            >
              {withdrawMutation.isPending ? 'Withdrawing...' : 'Withdraw'}
            </Button>
            <Button
              onClick={() => onOpenChange(false)}
              variant='outline'
              disabled={withdrawMutation.isPending}
              className='w-full sm:w-auto'
            >
              Cancel
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
