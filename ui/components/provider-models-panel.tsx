import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Database, Pencil, Plus, Trash2 } from 'lucide-react';
import type { AdminModel, ProviderModels } from '@/lib/api/services/admin';
import { ProviderModelRow } from '@/components/provider-model-row';

interface ProviderModelsPanelProps {
  isLoading: boolean;
  providerModels: ProviderModels | null;
  onBatchOverride: () => void;
  onAddModel: () => void;
  onEditModel: (model: AdminModel) => void;
  onDeleteModel: (modelId: string) => void;
  onOverrideModel: (model: AdminModel) => void;
  isDeletingModel: boolean;
}

export function ProviderModelsPanel({
  isLoading,
  providerModels,
  onBatchOverride,
  onAddModel,
  onEditModel,
  onDeleteModel,
  onOverrideModel,
  isDeletingModel,
}: ProviderModelsPanelProps) {
  if (isLoading) {
    return (
      <div className='space-y-2'>
        <Skeleton className='h-[40px] w-full' />
        <Skeleton className='h-[40px] w-full' />
      </div>
    );
  }

  if (!providerModels) {
    return null;
  }

  return (
    <Tabs
      defaultValue={providerModels.remote_models.length > 0 ? 'provided' : 'custom'}
      className='w-full'
    >
      <TabsList className='grid w-full grid-cols-2'>
        <TabsTrigger value='provided' className='text-xs sm:text-sm'>
          <span className='hidden sm:inline'>Provided Models</span>
          <span className='sm:hidden'>Provided</span>
          <Badge variant='secondary' className='ml-1 text-xs sm:ml-2'>
            {providerModels.remote_models.length}
          </Badge>
        </TabsTrigger>
        <TabsTrigger value='custom' className='text-xs sm:text-sm'>
          <span className='hidden sm:inline'>Custom Models</span>
          <span className='sm:hidden'>Custom</span>
          <Badge variant='secondary' className='ml-1 text-xs sm:ml-2'>
            {providerModels.db_models.length}
          </Badge>
        </TabsTrigger>
      </TabsList>

      <TabsContent value='custom' className='mt-4 space-y-2'>
        <div className='flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between'>
          {providerModels.db_models.length > 0 && (
            <p className='text-muted-foreground text-sm'>
              Custom models override or extend the provider&apos;s catalog.
            </p>
          )}
          <div className='flex w-full flex-wrap gap-2 sm:w-auto sm:justify-end'>
            <Button
              variant='outline'
              size='sm'
              onClick={onBatchOverride}
              className='w-full sm:w-auto'
            >
              <Database className='mr-2 h-4 w-4' />
              Batch Override
            </Button>
            <Button
              variant='outline'
              size='sm'
              onClick={onAddModel}
              className='w-full sm:w-auto'
            >
              <Plus className='mr-2 h-4 w-4' />
              Add Custom Model
            </Button>
          </div>
        </div>

        {providerModels.db_models.length === 0 ? (
          <div className='text-muted-foreground py-4 text-center text-sm'>
            No custom models configured
          </div>
        ) : (
          <div className='space-y-2'>
            {providerModels.db_models.map((model) => (
              <ProviderModelRow
                key={model.id}
                model={model}
                showEnabledState
                actions={
                  <>
                    <Button
                      variant='ghost'
                      size='icon'
                      className='h-8 w-8'
                      onClick={() => onEditModel(model)}
                    >
                      <Pencil className='h-4 w-4' />
                    </Button>
                    <Button
                      variant='ghost'
                      size='icon'
                      className='text-destructive hover:text-destructive h-8 w-8'
                      onClick={() => onDeleteModel(model.id)}
                      disabled={isDeletingModel}
                    >
                      <Trash2 className='h-4 w-4' />
                    </Button>
                  </>
                }
              />
            ))}
          </div>
        )}
      </TabsContent>

      <TabsContent value='provided' className='mt-4 space-y-2'>
        {providerModels.remote_models.length > 0 ? (
          <>
            <p className='text-muted-foreground mb-3 text-sm'>
              Models automatically discovered from the provider&apos;s catalog.
            </p>
            <div className='space-y-2'>
              {providerModels.remote_models.map((model) => (
                <ProviderModelRow
                  key={model.id}
                  model={model}
                  actions={
                    <Button
                      variant='outline'
                      size='sm'
                      className='h-7 w-full text-xs sm:w-auto'
                      onClick={() => onOverrideModel(model)}
                    >
                      <Plus className='mr-1 h-3 w-3' />
                      Override
                    </Button>
                  }
                />
              ))}
            </div>
          </>
        ) : (
          <div className='text-muted-foreground py-4 text-center text-sm'>
            No provided models available
          </div>
        )}
      </TabsContent>
    </Tabs>
  );
}
