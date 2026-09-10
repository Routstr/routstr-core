import { Calendar } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';

interface KeyOptionsProps {
  validityDate: string;
  setValidityDate: (val: string) => void;
}

export function KeyOptions({ validityDate, setValidityDate }: KeyOptionsProps) {
  return (
    <div className='grid gap-4 sm:grid-cols-3'>
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
    </div>
  );
}
