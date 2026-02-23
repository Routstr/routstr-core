'use client';

import type { Dispatch, SetStateAction } from 'react';
import type {
  CreateUpstreamProvider,
  ProviderType,
} from '@/lib/api/services/admin';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';

interface ProviderFormFieldsProps {
  mode: 'create' | 'edit';
  formData: CreateUpstreamProvider;
  setFormData: Dispatch<SetStateAction<CreateUpstreamProvider>>;
  providerTypes: ProviderType[];
  providerFeePlaceholder: string;
  docsLinkClassName: string;
  canCreateAccount: boolean;
  isCreatingAccount: boolean;
  onCreateAccount: () => void;
}

export function ProviderFormFields({
  mode,
  formData,
  setFormData,
  providerTypes,
  providerFeePlaceholder,
  docsLinkClassName,
  canCreateAccount,
  isCreatingAccount,
  onCreateAccount,
}: ProviderFormFieldsProps) {
  const idPrefix = mode === 'edit' ? 'edit_' : '';
  const providerType = providerTypes.find(
    (pt) => pt.id === formData.provider_type
  );
  const hasFixedBaseUrl = providerType?.fixed_base_url || false;
  const platformUrl = providerType?.platform_url || null;
  const isGenericType = (type: ProviderType) =>
    type.id.toLowerCase() === 'generic';
  const nonGenericTypes = providerTypes.filter((type) => !isGenericType(type));
  const genericType = providerTypes.find((type) => isGenericType(type));
  const apiKeyLabel =
    mode === 'edit' ? 'API Key (leave blank to keep current)' : 'API Key';
  const apiKeyPlaceholder =
    mode === 'edit' ? 'Leave blank to keep current' : 'sk-...';

  return (
    <div className='grid gap-4 py-4'>
      <div className='grid gap-2'>
        <Label htmlFor={`${idPrefix}provider_type`}>Provider Type</Label>
        <Select
          value={formData.provider_type}
          onValueChange={(value) => {
            const selectedType = providerTypes.find((pt) => pt.id === value);
            setFormData((prev) => ({
              ...prev,
              provider_type: value,
              base_url: selectedType?.default_base_url || '',
              provider_fee: value === 'openrouter' ? 1.06 : 1.01,
            }));
          }}
        >
          <SelectTrigger id={`${idPrefix}provider_type`}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {nonGenericTypes.map((type) => (
              <SelectItem key={type.id} value={type.id}>
                {type.name}
              </SelectItem>
            ))}
            {genericType ? (
              <>
                {nonGenericTypes.length > 0 ? (
                  <SelectSeparator className='my-1.5 opacity-70' />
                ) : null}
                <SelectItem value={genericType.id} className='font-medium'>
                  Custom
                </SelectItem>
              </>
            ) : null}
          </SelectContent>
        </Select>
      </div>

      <div className='grid gap-2'>
        <Label htmlFor={`${idPrefix}base_url`}>Base URL</Label>
        <Input
          id={`${idPrefix}base_url`}
          value={formData.base_url}
          onChange={(e) =>
            setFormData((prev) => ({ ...prev, base_url: e.target.value }))
          }
          placeholder='https://api.example.com/v1'
          disabled={hasFixedBaseUrl}
          className={hasFixedBaseUrl ? 'cursor-not-allowed opacity-60' : ''}
        />
      </div>

      <div className='grid gap-2'>
        <div className='flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between'>
          <Label htmlFor={`${idPrefix}api_key`}>{apiKeyLabel}</Label>
          {mode === 'create' && canCreateAccount ? (
            <Button
              type='button'
              variant='outline'
              size='sm'
              onClick={onCreateAccount}
              disabled={isCreatingAccount}
              className='h-6 w-full text-xs sm:w-auto'
            >
              {isCreatingAccount ? 'Creating...' : 'Create Account'}
            </Button>
          ) : (
            platformUrl && (
              <a
                href={platformUrl}
                target='_blank'
                rel='noopener noreferrer'
                className={`${docsLinkClassName} break-all`}
              >
                Get Your API Key Here →
              </a>
            )
          )}
        </div>
        <Input
          id={`${idPrefix}api_key`}
          type='password'
          value={formData.api_key}
          onChange={(e) =>
            setFormData((prev) => ({ ...prev, api_key: e.target.value }))
          }
          placeholder={apiKeyPlaceholder}
        />
      </div>

      {formData.provider_type === 'azure' && (
        <div className='grid gap-2'>
          <Label htmlFor={`${idPrefix}api_version`}>API Version</Label>
          <Input
            id={`${idPrefix}api_version`}
            value={formData.api_version || ''}
            onChange={(e) =>
              setFormData((prev) => ({
                ...prev,
                api_version: e.target.value || null,
              }))
            }
            placeholder='2024-02-15-preview'
          />
        </div>
      )}

      <div className='flex items-center space-x-2'>
        <Switch
          id={`${idPrefix}enabled`}
          checked={formData.enabled}
          onCheckedChange={(checked) =>
            setFormData((prev) => ({ ...prev, enabled: checked }))
          }
        />
        <Label htmlFor={`${idPrefix}enabled`}>Enabled</Label>
      </div>

      <div className='grid gap-2'>
        <Label htmlFor={`${idPrefix}provider_fee`}>
          Provider Fee (Multiplier)
        </Label>
        <Input
          id={`${idPrefix}provider_fee`}
          type='number'
          step='0.001'
          min='1.0'
          value={formData.provider_fee || ''}
          onChange={(e) =>
            setFormData((prev) => ({
              ...prev,
              provider_fee: e.target.value
                ? parseFloat(e.target.value)
                : undefined,
            }))
          }
          placeholder={providerFeePlaceholder}
        />
        <p className='text-muted-foreground text-xs'>
          1.01 means +1% e.g. currency exchange, card fees, etc.
        </p>
      </div>
    </div>
  );
}
