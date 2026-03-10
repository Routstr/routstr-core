'use client';

import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';

interface ProviderSettings {
  topup_mint_url?: string;
  auto_topup?: boolean;
  topup_threshold?: number;
  topup_amount_limit?: number;
  refund_on_expiry?: boolean;
  [key: string]: unknown;
}

interface RoutstrNodeSettingsProps {
  settings: ProviderSettings;
  onSettingsChange: (settings: ProviderSettings) => void;
  availableMints: string[];
  idPrefix?: string;
}

export function RoutstrNodeSettings({
  settings,
  onSettingsChange,
  availableMints,
  idPrefix = '',
}: RoutstrNodeSettingsProps) {
  const prefix = idPrefix ? `${idPrefix}_` : '';

  const update = (patch: Partial<ProviderSettings>) => {
    onSettingsChange({ ...settings, ...patch });
  };

  return (
    <div className='bg-muted/30 grid gap-4 rounded-lg border p-4'>
      <Label className='text-sm font-semibold'>Routstr Node Settings</Label>

      <div className='grid gap-3'>
        <div className='grid gap-2'>
          <Label htmlFor={`${prefix}topup_mint_url`} className='text-xs'>
            Top-up Mint
          </Label>
          <Select
            value={settings.topup_mint_url || ''}
            onValueChange={(value) => update({ topup_mint_url: value })}
          >
            <SelectTrigger
              id={`${prefix}topup_mint_url`}
              className='h-8 text-xs'
            >
              <SelectValue placeholder='Select a mint from your node configuration' />
            </SelectTrigger>
            <SelectContent>
              {availableMints.length > 0 ? (
                availableMints.map((mint) => (
                  <SelectItem key={mint} value={mint} className='text-xs'>
                    {mint}
                  </SelectItem>
                ))
              ) : (
                <SelectItem value='none' disabled className='text-xs'>
                  No mints configured in global settings
                </SelectItem>
              )}
            </SelectContent>
          </Select>
          <p className='text-muted-foreground text-[10px]'>
            The token for top-up will be created from this mint.
          </p>
        </div>

        <div className='flex items-center justify-between opacity-50'>
          <Label htmlFor={`${prefix}auto_topup`} className='text-sm'>
            Enable Auto Top-up
            <span className='text-muted-foreground ml-2 text-xs'>(Coming Soon)</span>
          </Label>
          <Switch
            id={`${prefix}auto_topup`}
            checked={false}
            disabled
          />
        </div>

        {settings.auto_topup && (
          <div className='border-primary/20 grid gap-4 border-l-2 pt-2 pl-4'>
            <div className='grid gap-2'>
              <Label
                htmlFor={`${prefix}topup_threshold`}
                className='text-xs font-medium'
              >
                When credits are below (Sats)
              </Label>
              <Input
                id={`${prefix}topup_threshold`}
                type='number'
                className='h-9'
                placeholder='e.g. 1000'
                value={settings.topup_threshold || ''}
                onChange={(e) =>
                  update({ topup_threshold: parseInt(e.target.value) })
                }
              />
            </div>

            <div className='grid gap-2'>
              <Label
                htmlFor={`${prefix}topup_amount_limit`}
                className='text-xs font-medium'
              >
                Purchase this amount (Sats)
              </Label>
              <Input
                id={`${prefix}topup_amount_limit`}
                type='number'
                className='h-9'
                placeholder='e.g. 5000'
                value={settings.topup_amount_limit || ''}
                onChange={(e) =>
                  update({ topup_amount_limit: parseInt(e.target.value) })
                }
              />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
