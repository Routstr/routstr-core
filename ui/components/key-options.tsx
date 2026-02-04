import { Zap, Calendar, Shield } from 'lucide-react';
import { Input } from '@/components/ui/input';

interface KeyOptionsProps {
  balanceLimit: string;
  setBalanceLimit: (val: string) => void;
  validityDate: string;
  setValidityDate: (val: string) => void;
  balanceLimitReset: string;
  setBalanceLimitReset: (val: string) => void;
}

export function KeyOptions({
  balanceLimit,
  setBalanceLimit,
  validityDate,
  setValidityDate,
  balanceLimitReset,
  setBalanceLimitReset,
}: KeyOptionsProps) {
  return (
    <div className='grid gap-4 sm:grid-cols-3'>
      <div className='space-y-2'>
        <label className='text-muted-foreground flex items-center gap-1.5 text-[0.7rem] tracking-wider uppercase'>
          <Zap className='h-3 w-3' />
          Balance Limit (mSats)
        </label>
        <Input
          type='number'
          placeholder='No limit'
          value={balanceLimit}
          onChange={(e) => setBalanceLimit(e.target.value)}
          className='h-9 text-xs'
        />
      </div>

      <div className='space-y-2'>
        <label className='text-muted-foreground flex items-center gap-1.5 text-[0.7rem] tracking-wider uppercase'>
          <Calendar className='h-3 w-3' />
          Validity Date
        </label>
        <Input
          type='date'
          value={validityDate}
          onChange={(e) => setValidityDate(e.target.value)}
          className='h-9 text-xs'
        />
      </div>

      <div className='space-y-2'>
        <label className='text-muted-foreground flex items-center gap-1.5 text-[0.7rem] tracking-wider uppercase'>
          <Shield className='h-3 w-3' />
          Reset Policy
        </label>
        <select
          value={balanceLimitReset}
          onChange={(e) => setBalanceLimitReset(e.target.value)}
          className='bg-background flex h-9 w-full rounded-md border border-input px-3 py-1 text-xs shadow-sm transition-colors'
        >
          <option value=''>None</option>
          <option value='daily'>Daily</option>
          <option value='weekly'>Weekly</option>
          <option value='monthly'>Monthly</option>
        </select>
      </div>
    </div>
  );
}
