'use client';

import { ModelSelector } from '@/components/ModelSelector';
import { ModelTester } from '@/components/ModelTester';
import { ApiEndpointTester } from '@/components/ApiEndpointTester';
import { ModelSearchFilter } from '@/components/ModelSearchFilter';
import { SidebarInset, SidebarProvider } from '@/components/ui/sidebar';
import { AppSidebar } from '@/components/app-sidebar';
import { SiteHeader } from '@/components/site-header';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useQuery } from '@tanstack/react-query';
import { AdminService } from '@/lib/api/services/admin';
import { Skeleton } from '@/components/ui/skeleton';
import { AlertCircle, Users, Globe } from 'lucide-react';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { useMemo, useState } from 'react';
import type { Model } from '@/lib/api/schemas/models';
import { groupAndSortModelsByProvider } from '@/lib/utils/modelSort';

export default function ModelsPage() {
  const [filteredModels, setFilteredModels] = useState<Model[]>([]);

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

  const groupedModels = useMemo(() => {
    if (!models) return {};
    return groupAndSortModelsByProvider(models);
  }, [models]);

  const groupDataMap = useMemo(() => {
    return new Map(groups.map((group) => [group.provider, group]));
  }, [groups]);

  const providerInfo = useMemo(() => {
    return Object.entries(groupedModels).map(([provider, providerModels]) => {
      const groupData = groupDataMap.get(provider);
      const activeModels = providerModels.filter(
        (m) => m.isEnabled && !m.soft_deleted
      ).length;
      const totalModels = providerModels.length;

      return {
        provider,
        activeModels,
        totalModels,
        groupData,
        hasGroupUrl: !!groupData?.group_url,
        hasGroupApiKey: !!groupData?.group_api_key,
      };
    });
  }, [groupedModels, groupDataMap]);

  return (
    <SidebarProvider>
      <AppSidebar variant='inset' />
      <SidebarInset>
        <SiteHeader />
        <div className='flex flex-1 flex-col'>
          <div className='@container/main flex flex-1 flex-col gap-4 p-4 md:gap-8 md:p-8'>
            <div className='mb-6 flex items-center justify-between'>
              <h1 className='text-2xl font-bold tracking-tight'>
                Model Management & API Testing
              </h1>
            </div>

            <Tabs defaultValue='manage' className='w-full'>
              <TabsList className='grid w-full grid-cols-3'>
                <TabsTrigger value='manage'>Manage Models</TabsTrigger>
                {/*<TabsTrigger value='test-basic'>Basic Testing</TabsTrigger>
                <TabsTrigger value='test-api'>API Endpoints</TabsTrigger> */}
              </TabsList>

              <TabsContent value='manage' className='space-y-4'>
                <div className='text-muted-foreground text-sm'>
                  Manage your AI models organized by provider groups. Configure
                  API keys, and organize models by provider groups.
                </div>

                {isLoadingModels ? (
                  <div className='space-y-4'>
                    <Skeleton className='h-[60px] w-full' />
                    <Skeleton className='h-[400px] w-full' />
                  </div>
                ) : modelsError ? (
                  <Alert variant='destructive'>
                    <AlertCircle className='h-4 w-4' />
                    <AlertDescription>
                      Failed to load models. Please try refreshing the page.
                    </AlertDescription>
                  </Alert>
                ) : (
                  <Tabs defaultValue='all' className='w-full'>
                    <div className='space-y-4'>
                      {/* Provider Tabs Navigation */}
                      <div className='overflow-x-auto rounded-lg border p-1'>
                        <TabsList className='grid w-full max-w-full min-w-max auto-cols-fr grid-flow-col gap-1 sm:gap-2'>
                          <TabsTrigger
                            value='all'
                            className='flex items-center gap-1 text-xs whitespace-nowrap sm:gap-2 sm:text-sm'
                          >
                            <Globe className='h-3 w-3 sm:h-4 sm:w-4' />
                            <span className='hidden sm:inline'>All Models</span>
                            <span className='sm:hidden'>All</span>
                            <Badge variant='secondary' className='ml-1 text-xs'>
                              {models.length}
                            </Badge>
                          </TabsTrigger>
                          {providerInfo.map(
                            ({ provider, activeModels, totalModels }) => (
                              <TabsTrigger
                                key={provider}
                                value={provider}
                                className='flex min-w-fit items-center gap-1 text-xs whitespace-nowrap sm:gap-2 sm:text-sm'
                              >
                                <Users className='h-3 w-3 sm:h-4 sm:w-4' />
                                <span className='max-w-20 truncate sm:max-w-none'>
                                  {provider}
                                </span>
                                <div className='flex items-center gap-1'>
                                  <Badge
                                    variant='secondary'
                                    className='ml-1 text-xs'
                                  >
                                    {activeModels}/{totalModels}
                                  </Badge>
                                </div>
                              </TabsTrigger>
                            )
                          )}
                        </TabsList>
                      </div>

                      {/* All Models Tab */}
                      <TabsContent value='all'>
                        <div className='space-y-4'>
                          <div className='text-muted-foreground text-sm'>
                            Overview of all models across all provider groups.
                          </div>
                          <ModelSearchFilter
                            models={models}
                            onFilteredModelsChange={setFilteredModels}
                          />
                          <ModelSelector
                            filteredModels={filteredModels}
                            showDeleteAllButton={true}
                          />
                        </div>
                      </TabsContent>

                      {Object.entries(groupedModels).map(
                        ([provider, providerModels]) => {
                          const groupData = groupDataMap.get(provider);

                          return (
                            <TabsContent key={provider} value={provider}>
                              <div className='space-y-4'>
                                <div className='flex items-center justify-between'>
                                  <div>
                                    <h3 className='flex items-center gap-2 text-lg font-semibold'>
                                      <Users className='h-5 w-5' />
                                      {provider}
                                    </h3>
                                    <div className='text-muted-foreground flex items-center gap-4 text-sm'>
                                      {providerModels.filter(
                                        (m) => m.soft_deleted
                                      ).length > 0 && (
                                        <span className='text-orange-600'>
                                          {
                                            providerModels.filter(
                                              (m) => m.soft_deleted
                                            ).length
                                          }{' '}
                                          disabled
                                        </span>
                                      )}
                                      {groupData?.group_url && (
                                        <span className='flex items-center gap-1'>
                                          <Globe className='h-3 w-3' />
                                          {groupData.group_url}
                                        </span>
                                      )}
                                    </div>
                                  </div>
                                </div>
                                <ModelSelector
                                  filterProvider={provider}
                                  groupData={groupData}
                                  showProviderActions={true}
                                  showDeleteAllButton={false}
                                />
                              </div>
                            </TabsContent>
                          );
                        }
                      )}
                    </div>
                  </Tabs>
                )}
              </TabsContent>

              <TabsContent value='test-basic' className='space-y-4'>
                <div className='text-muted-foreground text-sm'>
                  Test model credentials and connectivity with basic chat
                  completion requests through the secure proxy (resolves CORS
                  and Docker network issues). Models can be tested even without
                  API keys configured (useful for free models or when
                  authentication is handled elsewhere).
                </div>

                {isLoadingModels ? (
                  <div className='space-y-4'>
                    <Skeleton className='h-[200px] w-full' />
                    <Skeleton className='h-[100px] w-full' />
                  </div>
                ) : modelsError ? (
                  <Alert variant='destructive'>
                    <AlertCircle className='h-4 w-4' />
                    <AlertDescription>
                      Failed to load models for testing. Please try refreshing
                      the page.
                    </AlertDescription>
                  </Alert>
                ) : (
                  <ModelTester models={models} />
                )}
              </TabsContent>

              <TabsContent value='test-api' className='space-y-4'>
                <div className='text-muted-foreground text-sm'>
                  Comprehensive testing of all OpenAI API endpoints including
                  chat completions, embeddings, image generation, audio
                  synthesis, and model listing through the secure proxy
                  (resolves CORS and Docker network issues). Models can be
                  tested with or without API keys configured.
                </div>

                {isLoadingModels ? (
                  <div className='space-y-4'>
                    <Skeleton className='h-[300px] w-full' />
                    <Skeleton className='h-[200px] w-full' />
                  </div>
                ) : modelsError ? (
                  <Alert variant='destructive'>
                    <AlertCircle className='h-4 w-4' />
                    <AlertDescription>
                      Failed to load models for API testing. Please try
                      refreshing the page.
                    </AlertDescription>
                  </Alert>
                ) : (
                  <ApiEndpointTester models={models} />
                )}
              </TabsContent>
            </Tabs>
          </div>
        </div>
      </SidebarInset>
    </SidebarProvider>
  );
}
