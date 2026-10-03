'use client';

import { useMemo, useState } from 'react';
import { AlertCircle } from 'lucide-react';
import type { Model } from '@/lib/api/schemas/models';
import { useModelsWithProviders } from '@/lib/hooks/use-models-with-providers';
import { groupAndSortModelsByProvider } from '@/lib/utils/model-sort';
import { AppPageShell } from '@/components/app-page-shell';
import { PageHeader } from '@/components/page-header';
import { ModelSelector } from '@/components/model-selector';
import { ModelSearchFilter } from '@/components/model-search-filter';
import { Alert, AlertDescription } from '@/components/ui/alert';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';

export function ModelsPage() {
  const [filteredModels, setFilteredModels] = useState<Model[] | undefined>(
    undefined
  );
  const [selectedProviderScope, setSelectedProviderScope] =
    useState<string>('all');

  const {
    models,
    groups,
    isLoading: isLoadingModels,
    error: modelsError,
  } = useModelsWithProviders();

  const groupedModels = useMemo(
    () => groupAndSortModelsByProvider(models),
    [models]
  );

  const groupDataMap = useMemo(
    () => new Map(groups.map((group) => [group.provider, group])),
    [groups]
  );

  const providerInfo = useMemo(() => {
    const allProviders = new Set([
      ...Object.keys(groupedModels),
      ...groups.map((group) => group.provider),
    ]);

    return Array.from(allProviders)
      .map((provider) => {
        const providerModels = groupedModels[provider] || [];
        const groupData = groupDataMap.get(provider);

        return {
          provider,
          totalModels: providerModels.length,
          disabledModels: providerModels.filter((model) => model.soft_deleted)
            .length,
          groupData,
        };
      })
      .sort((a, b) => a.provider.localeCompare(b.provider));
  }, [groupDataMap, groupedModels, groups]);

  const activeProviderScope = useMemo(() => {
    if (selectedProviderScope === 'all') {
      return 'all';
    }

    const providerExists = providerInfo.some(
      (provider) => provider.provider === selectedProviderScope
    );

    return providerExists ? selectedProviderScope : 'all';
  }, [providerInfo, selectedProviderScope]);

  const selectedProviderGroup =
    activeProviderScope === 'all'
      ? undefined
      : groupDataMap.get(activeProviderScope);

  const scopedModels = useMemo(() => {
    if (activeProviderScope === 'all') {
      return models;
    }

    return models.filter((model) => model.provider === activeProviderScope);
  }, [activeProviderScope, models]);

  return (
    <AppPageShell contentClassName='mx-auto w-full max-w-5xl'>
      <div className='space-y-3 sm:space-y-4'>
        <PageHeader
          title='Model Management'
          description='Manage provider model catalogs.'
        />

        {isLoadingModels ? (
          <div className='space-y-4'>
            <Skeleton className='h-16 w-full' />
            <Skeleton className='h-[420px] w-full' />
          </div>
        ) : modelsError ? (
          <Alert variant='destructive'>
            <AlertCircle className='h-4 w-4' />
            <AlertDescription>
              Failed to load models. Please try refreshing the page.
            </AlertDescription>
          </Alert>
        ) : (
          <div className='space-y-3 sm:space-y-4'>
            <div className='flex flex-col gap-2 sm:gap-2.5 md:flex-row md:items-center'>
              <Select
                value={activeProviderScope}
                onValueChange={(value) => {
                  setSelectedProviderScope(value);
                  setFilteredModels(undefined);
                }}
              >
                <SelectTrigger className='h-8 w-full md:w-[220px]'>
                  <SelectValue placeholder='Provider scope' />
                </SelectTrigger>
                <SelectContent align='start'>
                  <SelectItem value='all'>
                    All providers ({models.length})
                  </SelectItem>
                  {providerInfo.map(({ provider, totalModels }) => (
                    <SelectItem key={provider} value={provider}>
                      {provider} ({totalModels})
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>

              <ModelSearchFilter
                models={scopedModels}
                onFilteredModelsChange={setFilteredModels}
                className='w-full min-w-0 flex-1'
              />
            </div>

            <ModelSelector
              filterProvider={
                activeProviderScope === 'all' ? undefined : activeProviderScope
              }
              groupData={selectedProviderGroup}
              filteredModels={filteredModels}
              showDeleteAllButton={activeProviderScope === 'all'}
            />
          </div>
        )}
      </div>
    </AppPageShell>
  );
}
