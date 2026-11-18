'use client';

import { useState } from 'react';
import { CheckCircle, Copy, Download } from 'lucide-react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { useCopyToClipboard } from '@/hooks/use-copy-to-clipboard';
import { downloadText } from '@/lib/utils';

interface RefundTokenDialogProps {
  // A refund token the upstream already debited but the local wallet could
  // not receive. Nothing else holds it, so it is shown until confirmed saved.
  token: string | null;
  message?: string;
  onSaved: () => void;
}

export function RefundTokenDialog({
  token,
  message,
  onSaved,
}: RefundTokenDialogProps) {
  const [confirmedSaved, setConfirmedSaved] = useState(false);
  const { copied, copy } = useCopyToClipboard();

  const handleSaved = () => {
    setConfirmedSaved(false);
    onSaved();
  };

  return (
    // Escape, backdrop and X are ignored: closing must not drop the token
    // before the operator confirms it is saved.
    <Dialog open={token !== null}>
      <DialogContent className='sm:max-w-xl' showCloseButton={false}>
        <DialogHeader>
          <DialogTitle>Save the refund token</DialogTitle>
          <DialogDescription>
            The upstream node refunded the key, but this node could not receive
            the token
            {message ? ` (${message})` : ''}. This token is the money. Anyone
            holding it can redeem it, so save it before closing.
          </DialogDescription>
        </DialogHeader>

        <div className='space-y-4'>
          <Textarea
            readOnly
            value={token ?? ''}
            onFocus={(e) => e.target.select()}
            className='font-mono text-xs leading-relaxed'
            rows={6}
          />

          <div className='flex flex-col gap-2 sm:flex-row'>
            <Button
              onClick={() => token && copy(token)}
              className='w-full flex-1'
              variant={copied ? 'outline' : 'default'}
            >
              {copied ? (
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
              onClick={() =>
                token &&
                downloadText(`cashu-refund-token-${Date.now()}.txt`, token)
              }
              variant='outline'
              className='w-full flex-1'
            >
              <Download className='mr-2 h-4 w-4' />
              Download .txt
            </Button>
          </div>

          <div className='flex items-center gap-2'>
            <Checkbox
              id='refund-token-saved'
              checked={confirmedSaved}
              onCheckedChange={(checked) => setConfirmedSaved(checked === true)}
            />
            <Label htmlFor='refund-token-saved' className='text-sm font-normal'>
              I have saved this token
            </Label>
          </div>

          <Button
            onClick={handleSaved}
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
