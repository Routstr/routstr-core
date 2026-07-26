'use client';

import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';

interface ProviderSettings {
  auto_topup?: boolean;
  topup_threshold?: number;
  topup_amount_limit?: number;
  [key: string]: unknown;
}

interface PPQAutoTopupSettingsProps {
  settings: ProviderSettings;
  onSettingsChange: (settings: ProviderSettings) => void;
  idPrefix?: string;
}

export function PPQAutoTopupSettings({
  settings,
  onSettingsChange,
  idPrefix = '',
}: PPQAutoTopupSettingsProps) {
  const prefix = idPrefix ? `${idPrefix}_` : '';
  const update = (patch: Partial<ProviderSettings>) =>
    onSettingsChange({ ...settings, ...patch });

  /**
   * Clearing the field yields '' and parse* yields NaN, which JSON.stringify
   * turns into null. Drop the key instead so the server rejects a missing
   * value rather than storing a broken one.
   */
  const updateNumber = (
    key: 'topup_threshold' | 'topup_amount_limit',
    raw: string,
    parse: (value: string) => number
  ) => {
    const next = { ...settings };
    const parsed = parse(raw);
    if (raw === '' || Number.isNaN(parsed)) {
      delete next[key];
    } else {
      next[key] = parsed;
    }
    onSettingsChange(next);
  };

  const threshold = settings.topup_threshold;
  const amount = settings.topup_amount_limit;
  const thresholdError =
    threshold !== undefined && threshold <= 0
      ? 'Must be greater than 0'
      : undefined;
  const amountError =
    amount !== undefined && (amount < 1 || amount > 500)
      ? 'Must be between 1 and 500 USD'
      : undefined;

  return (
    <div className='bg-muted/30 grid gap-4 rounded-lg border p-4'>
      <Label className='text-sm font-semibold'>PPQ Auto Top-up</Label>

      <div className='flex items-center justify-between'>
        <Label htmlFor={`${prefix}ppq_auto_topup`} className='text-sm'>
          Enable Auto Top-up
        </Label>
        <Switch
          id={`${prefix}ppq_auto_topup`}
          checked={!!settings.auto_topup}
          onCheckedChange={(checked) => update({ auto_topup: checked })}
        />
      </div>

      {settings.auto_topup && (
        <div className='border-primary/20 grid gap-4 border-l-2 pt-2 pl-4'>
          <div className='grid gap-2'>
            <Label
              htmlFor={`${prefix}ppq_topup_threshold`}
              className='text-xs font-medium'
            >
              When credits are below (USD)
            </Label>
            <Input
              id={`${prefix}ppq_topup_threshold`}
              type='number'
              min='0.01'
              step='0.01'
              className='h-9'
              placeholder='e.g. 5'
              value={settings.topup_threshold ?? ''}
              aria-invalid={Boolean(thresholdError)}
              onChange={(e) =>
                updateNumber('topup_threshold', e.target.value, parseFloat)
              }
            />
            {thresholdError && (
              <p className='text-destructive text-[10px]'>{thresholdError}</p>
            )}
          </div>

          <div className='grid gap-2'>
            <Label
              htmlFor={`${prefix}ppq_topup_amount_limit`}
              className='text-xs font-medium'
            >
              Purchase this amount (USD)
            </Label>
            <Input
              id={`${prefix}ppq_topup_amount_limit`}
              type='number'
              min='1'
              max='500'
              step='1'
              className='h-9'
              placeholder='e.g. 10'
              value={settings.topup_amount_limit ?? ''}
              aria-invalid={Boolean(amountError)}
              onChange={(e) =>
                updateNumber('topup_amount_limit', e.target.value, parseInt)
              }
            />
            {amountError && (
              <p className='text-destructive text-[10px]'>{amountError}</p>
            )}
          </div>

          <p className='text-muted-foreground text-[10px]'>
            Pays PPQ&apos;s Lightning invoice from the sufficiently funded Cashu
            mint with the highest available balance.
          </p>
        </div>
      )}
    </div>
  );
}
