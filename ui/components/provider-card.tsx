import type {
  AdminModel,
  ProviderModels,
  UpstreamProvider,
} from '@/lib/api/services/admin';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { ChevronDown, ChevronUp, Database, Pencil, Trash2 } from 'lucide-react';
import { ProviderBalance } from '@/components/provider-balance';
import { ProviderModelsPanel } from '@/components/provider-models-panel';

interface ProviderCardProps {
  provider: UpstreamProvider;
  isExpanded: boolean;
  canShowBalance: boolean;
  platformUrl: string | null;
  isModelsLoading: boolean;
  providerModels: ProviderModels | null;
  isDeletingModel: boolean;
  onToggleExpansion: () => void;
  onEditProvider: () => void;
  onDeleteProvider: () => void;
  onBatchOverride: () => void;
  onAddModel: () => void;
  onEditModel: (model: AdminModel) => void;
  onDeleteModel: (modelId: string) => void;
  onOverrideModel: (model: AdminModel) => void;
}

export function ProviderCard({
  provider,
  isExpanded,
  canShowBalance,
  platformUrl,
  isModelsLoading,
  providerModels,
  isDeletingModel,
  onToggleExpansion,
  onEditProvider,
  onDeleteProvider,
  onBatchOverride,
  onAddModel,
  onEditModel,
  onDeleteModel,
  onOverrideModel,
}: ProviderCardProps) {
  const hasDetails = Boolean(provider.api_version) || isExpanded;

  return (
    <Card>
      <CardHeader>
        <div className='flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between'>
          <div className='min-w-0 flex-1'>
            <div className='flex flex-col gap-2 sm:flex-row sm:items-center'>
              <CardTitle className='truncate'>
                {provider.provider_type}
              </CardTitle>
              <Badge
                variant={provider.enabled ? 'default' : 'secondary'}
                className='w-fit'
              >
                {provider.enabled ? 'Enabled' : 'Disabled'}
              </Badge>
            </div>
            <CardDescription className='break-all'>
              {provider.base_url}
            </CardDescription>
          </div>

          <div className='grid w-full grid-cols-2 gap-2 sm:flex sm:w-auto sm:flex-wrap sm:items-center sm:justify-end'>
            {canShowBalance && provider.api_key && (
              <div className='col-span-2 sm:col-auto'>
                <ProviderBalance
                  providerId={provider.id}
                  platformUrl={platformUrl}
                />
              </div>
            )}

            <Button
              variant='outline'
              size='sm'
              onClick={onToggleExpansion}
              className='col-span-2 justify-between sm:col-auto sm:justify-center'
            >
              <span className='inline-flex items-center gap-1.5'>
                <Database className='h-4 w-4' />
                <span>Models</span>
              </span>
              {isExpanded ? (
                <ChevronUp className='h-4 w-4' />
              ) : (
                <ChevronDown className='h-4 w-4' />
              )}
            </Button>

            <Button
              variant='outline'
              size='sm'
              onClick={onEditProvider}
              className='justify-center gap-1.5'
            >
              <Pencil className='h-4 w-4' />
              <span>Edit</span>
            </Button>

            <Button
              variant='outline'
              size='sm'
              onClick={onDeleteProvider}
              className='text-destructive hover:text-destructive justify-center gap-1.5'
            >
              <Trash2 className='h-4 w-4' />
              <span>Delete</span>
            </Button>
          </div>
        </div>
      </CardHeader>

      {hasDetails ? (
        <CardContent>
          <div className='space-y-3'>
            {provider.api_version && (
              <div className='flex flex-col gap-1 text-sm sm:flex-row sm:items-center sm:justify-between'>
                <span className='text-muted-foreground'>API Version:</span>
                <span className='font-mono break-all'>
                  {provider.api_version}
                </span>
              </div>
            )}

            {isExpanded && (
              <div className='mt-3 border-t pt-3'>
                <ProviderModelsPanel
                  isLoading={isModelsLoading}
                  providerModels={providerModels}
                  onBatchOverride={onBatchOverride}
                  onAddModel={onAddModel}
                  onEditModel={onEditModel}
                  onDeleteModel={onDeleteModel}
                  onOverrideModel={onOverrideModel}
                  isDeletingModel={isDeletingModel}
                />
              </div>
            )}
          </div>
        </CardContent>
      ) : null}
    </Card>
  );
}
