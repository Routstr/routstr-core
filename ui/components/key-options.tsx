import { Zap, Calendar, Shield } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

interface KeyOptionsProps {
  balanceLimit: string;
  setBalanceLimit: (val: string) => void;
  validityDate: string;
  setValidityDate: (val: string) => void;
  balanceLimitReset: string;
  setBalanceLimitReset: (val: string) => void;
  showBalanceLimit?: boolean;
}

export function KeyOptions({
  balanceLimit,
  setBalanceLimit,
  validityDate,
  setValidityDate,
  balanceLimitReset,
  setBalanceLimitReset,
  showBalanceLimit = true,
}: KeyOptionsProps) {
  return (
    <div className='grid gap-4 sm:grid-cols-3'>
      {showBalanceLimit && (
        <div className='space-y-2'>
          <Label className='text-muted-foreground flex items-center gap-1.5 text-[0.7rem] tracking-wider uppercase'>
            <Zap className='h-3 w-3' />
            Balance Limit (mSats)
          </Label>
          <Input
            type='number'
            placeholder='No limit'
            value={balanceLimit}
            onChange={(e) => setBalanceLimit(e.target.value)}
            className='h-9 text-xs'
            name='balance_limit_msats'
            autoComplete='off'
          />
        </div>
      )}

      <div className='space-y-2'>
        <Label className='text-muted-foreground flex items-center gap-1.5 text-[0.7rem] tracking-wider uppercase'>
          <Calendar className='h-3 w-3' />
          Validity Date
        </Label>
        <Input
          type='date'
          value={validityDate}
          onChange={(e) => setValidityDate(e.target.value)}
          className='h-9 text-xs'
          name='validity_date'
        />
      </div>

      <div className='space-y-2'>
        <Label className='text-muted-foreground flex items-center gap-1.5 text-[0.7rem] tracking-wider uppercase'>
          <Shield className='h-3 w-3' />
          Reset Policy
        </Label>
        <Select
          value={balanceLimitReset || 'none'}
          onValueChange={(value) =>
            setBalanceLimitReset(value === 'none' ? '' : value)
          }
        >
          <SelectTrigger className='h-9 w-full text-xs'>
            <SelectValue placeholder='None' />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value='none'>None</SelectItem>
            <SelectItem value='daily'>Daily</SelectItem>
            <SelectItem value='weekly'>Weekly</SelectItem>
            <SelectItem value='monthly'>Monthly</SelectItem>
          </SelectContent>
        </Select>
      </div>
    </div>
  );
}
