'use client';

import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { AlertCircle } from 'lucide-react';
import type { Model } from '@/lib/api/schemas/models';
import { AdminService } from '@/lib/api/services/admin';
import { groupAndSortModelsByProvider } from '@/lib/utils/model-sort';
import { AppPageShell } from '@/components/app-page-shell';
import { PageHeader } from '@/components/page-header';
import { ModelSelector } from '@/components/model-selector';
import { ModelTester } from '@/components/model-tester';
import { ApiEndpointTester } from '@/components/api-endpoint-tester';
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';

export function ModelsPage() {
  const [filteredModels, setFilteredModels] = useState<Model[] | undefined>(
    undefined
  );
  const [selectedProviderScope, setSelectedProviderScope] =
    useState<string>('all');

  const {
    data: modelsData,
    isLoading: isLoadingModels,
    error: modelsError,
  } = useQuery({
    queryKey: ['admin-models-with-providers'],
    queryFn: () => AdminService.getModelsWithProviders(),
    refetchOnWindowFocus: false,
  });

  const { models = [], groups = [] } = modelsData || {};

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
          description='Manage provider model catalogs and validate endpoints from one place.'
        />

        <Tabs defaultValue='manage' className='w-full gap-3 sm:gap-4'>
          <TabsList
            variant='line'
            className='w-full snap-x snap-mandatory justify-start gap-0.5 overflow-x-auto whitespace-nowrap [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden'
          >
            <TabsTrigger
              value='manage'
              className='h-9 snap-start px-2 text-[13px] sm:h-10 sm:px-2.5 sm:text-sm'
            >
              Manage Models
            </TabsTrigger>
            <TabsTrigger
              value='test-basic'
              className='h-9 snap-start px-2 text-[13px] sm:h-10 sm:px-2.5 sm:text-sm'
            >
              Basic Testing
            </TabsTrigger>
            <TabsTrigger
              value='test-api'
              className='h-9 snap-start px-2 text-[13px] sm:h-10 sm:px-2.5 sm:text-sm'
            >
              API Endpoints
            </TabsTrigger>
          </TabsList>

          <TabsContent value='manage' className='mt-0'>
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
                    activeProviderScope === 'all'
                      ? undefined
                      : activeProviderScope
                  }
                  groupData={selectedProviderGroup}
                  filteredModels={filteredModels}
                  showDeleteAllButton={activeProviderScope === 'all'}
                />
              </div>
            )}
          </TabsContent>

          <TabsContent value='test-basic' className='mt-0 space-y-3'>
            <div className='space-y-1'>
              <h3 className='text-base font-semibold'>
                Basic Credential Testing
              </h3>
              <p className='text-muted-foreground text-sm'>
                Run chat-completion checks through the secure proxy to validate
                model credentials and endpoint connectivity.
              </p>
            </div>
            {isLoadingModels ? (
              <div className='space-y-4'>
                <Skeleton className='h-[220px] w-full' />
                <Skeleton className='h-[120px] w-full' />
              </div>
            ) : modelsError ? (
              <Alert variant='destructive'>
                <AlertCircle className='h-4 w-4' />
                <AlertDescription>
                  Failed to load models for testing. Please try refreshing the
                  page.
                </AlertDescription>
              </Alert>
            ) : (
              <ModelTester models={models} />
            )}
          </TabsContent>

          <TabsContent value='test-api' className='mt-0 space-y-3'>
            <div className='space-y-1'>
              <h3 className='text-base font-semibold'>
                OpenAI Endpoint Testing
              </h3>
              <p className='text-muted-foreground text-sm'>
                Validate chat, embeddings, image, audio, and model-listing
                endpoints through the secure proxy.
              </p>
            </div>
            {isLoadingModels ? (
              <div className='space-y-4'>
                <Skeleton className='h-[320px] w-full' />
                <Skeleton className='h-[220px] w-full' />
              </div>
            ) : modelsError ? (
              <Alert variant='destructive'>
                <AlertCircle className='h-4 w-4' />
                <AlertDescription>
                  Failed to load models for API testing. Please try refreshing
                  the page.
                </AlertDescription>
              </Alert>
            ) : (
              <ApiEndpointTester models={models} />
            )}
          </TabsContent>
        </Tabs>
      </div>
    </AppPageShell>
  );
}
