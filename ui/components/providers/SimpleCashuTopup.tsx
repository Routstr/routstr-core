'use client';

import { type JSX, useState } from 'react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { AdminService } from '@/lib/api/services/admin';

interface SimpleCashuTopupProps {
  providerId: number;
  baseUrl: string;
  onSuccess?: () => void;
}

export function SimpleCashuTopup({
  providerId,
  onSuccess,
}: SimpleCashuTopupProps): JSX.Element {
  const [token, setToken] = useState('');
  const [isLoading, setIsLoading] = useState(false);

  const handleTopup = async () => {
    if (!token.trim()) {
      toast.error('Enter a Cashu token');
      return;
    }
    setIsLoading(true);
    try {
      // Use the backend to proxy the token topup
      // This is safer as the backend has the actual API key
      const response = await AdminService.topupProviderWithToken(
        providerId,
        token.trim()
      );
      if (!response.ok) throw new Error(response.message || 'Top-up failed');

      toast.success('Token redeemed successfully!');
      setToken('');
      onSuccess?.();
    } catch (e) {
      const error = e as Error;
      toast.error(error.message || 'Top-up failed');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className='bg-muted/20 space-y-3 rounded-lg border p-4'>
      <div className='space-y-2'>
        <Textarea
          placeholder='Paste Cashu token here...'
          value={token}
          onChange={(e) => setToken(e.target.value)}
          rows={2}
          className='font-mono text-xs'
        />
        <Button
          onClick={handleTopup}
          disabled={isLoading}
          size='sm'
          className='w-full'
        >
          {isLoading ? 'Redeeming...' : 'Redeem Token'}
        </Button>
      </div>
    </div>
  );
}
