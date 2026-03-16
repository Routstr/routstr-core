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
import {
  ChevronDown,
  ChevronUp,
  Database,
  Pencil,
  Trash2,
  Key,
  RotateCcw,
} from 'lucide-react';
import { ProviderBalance } from '@/components/provider-balance';
import { ProviderModelsPanel } from '@/components/provider-models-panel';
import { RoutstrCreateKeySection } from '@/components/providers/RoutstrCreateKeySection';
import { RoutstrProviderService } from '@/lib/api/services/routstr-provider';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { toast } from 'sonner';
import { cn } from '@/lib/utils';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';

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
  onUpdateApiKey: (newKey: string) => void;
  availableMints: string[];
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
  onUpdateApiKey,
}: ProviderCardProps) {
  const queryClient = useQueryClient();
  const [isKeyModalOpen, setIsKeyModalOpen] = useState(false);
  const hasDetails = Boolean(provider.api_version) || isExpanded;
  const isRoutstr = provider.provider_type === 'routstr';

  const refundMutation = useMutation({
    mutationFn: () => RoutstrProviderService.refundBalance(provider.id),
    onSuccess: (data) => {
      if (data.ok) {
        toast.success('Refund successful', { description: data.message });
        queryClient.invalidateQueries({
          queryKey: ['provider-balance', provider.id],
        });
        queryClient.invalidateQueries({ queryKey: ['balances'] });
      } else {
        toast.error('Refund failed', { description: data.message });
      }
    },
    onError: (error: Error) => {
      toast.error(`Refund error: ${error.message}`);
    },
  });

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
              <div
                className={cn(
                  'col-span-2 sm:col-auto',
                  provider.provider_type === 'routstr' && 'col-span-1'
                )}
              >
                <ProviderBalance
                  providerId={provider.id}
                  platformUrl={platformUrl}
                  isRoutstr={provider.provider_type === 'routstr'}
                  nodeUrl={provider.base_url}
                />
              </div>
            )}

            {isRoutstr && (
              <Button
                variant='outline'
                size='sm'
                onClick={() => setIsKeyModalOpen(true)}
                className={cn(
                  'justify-center gap-1.5',
                  canShowBalance && provider.api_key
                    ? 'col-span-1'
                    : 'col-span-2 sm:col-auto'
                )}
              >
                <Key className='h-4 w-4' />
                <span>New Key</span>
              </Button>
            )}

            {isRoutstr && provider.api_key && (
              <Button
                variant='outline'
                size='sm'
                onClick={() => refundMutation.mutate()}
                disabled={refundMutation.isPending}
                className='justify-center gap-1.5 text-orange-600 hover:text-orange-700 dark:text-orange-400'
                title='Refund balance to local wallet'
              >
                <RotateCcw
                  className={cn(
                    'h-4 w-4',
                    refundMutation.isPending && 'animate-spin'
                  )}
                />
                <span>Refund</span>
              </Button>
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

      <Dialog open={isKeyModalOpen} onOpenChange={setIsKeyModalOpen}>
        <DialogContent className='max-h-[90vh] overflow-y-auto sm:max-w-[500px]'>
          <DialogHeader>
            <DialogTitle>
              {provider.api_key
                ? 'Create New Key on Upstream Node'
                : 'Create API Key'}
            </DialogTitle>
            <DialogDescription>
              {provider.api_key
                ? 'Create a new API key on the upstream node. The remaining balance on the current key will be automatically refunded to your local wallet before it is replaced.'
                : 'Create an API key on the upstream Routstr node to enable balance, top-up, and refund operations.'}
            </DialogDescription>
          </DialogHeader>
          <div className='py-4'>
            <RoutstrCreateKeySection
              baseUrl={provider.base_url || ''}
              onApiKeyCreated={async (newApiKey) => {
                if (provider.api_key) {
                  try {
                    const result = await RoutstrProviderService.refundBalance(
                      provider.id
                    );
                    if (result.ok) {
                      toast.success('Old key refunded', {
                        description: result.message,
                      });
                    } else {
                      toast.warning('Refund skipped', {
                        description: result.message,
                      });
                    }
                  } catch (error) {
                    toast.warning(
                      `Could not refund old key: ${error instanceof Error ? error.message : 'Unknown error'}`
                    );
                  }
                }
                onUpdateApiKey(newApiKey);
                setIsKeyModalOpen(false);
              }}
            />
          </div>
        </DialogContent>
      </Dialog>

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
