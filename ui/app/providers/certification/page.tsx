'use client';

import { useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { useQueries, useQuery } from '@tanstack/react-query';
import {
  ArrowLeft,
  CheckCircle2,
  ChevronDown,
  Loader2,
  Play,
  RotateCcw,
  Server,
} from 'lucide-react';

import { AppPageShell } from '@/components/app-page-shell';
import { PageHeader } from '@/components/page-header';
import {
  ProviderCertificationResults,
  summarizeCertificationResults,
} from '@/components/provider-certification-results';
import {
  getCertificationModelNames,
  ProviderCertificationSetupPanel,
} from '@/components/provider-certification-setup';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Checkbox } from '@/components/ui/checkbox';
import {
  Command,
  CommandEmpty,
  CommandInput,
  CommandItem,
  CommandList,
} from '@/components/ui/command';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { runProviderCertification } from '@/hooks/use-provider-certification-runner';
import { AdminService } from '@/lib/api/services/admin';
import type {
  ProviderModels,
  UpstreamProvider,
} from '@/lib/api/services/admin';
import {
  buildModelRuns,
  countCertificationTargets,
  emptyCertificationSetup,
  getModelsNeedingPath,
} from '@/lib/provider-certification';
import type {
  CertificationProgress,
  ModelCertificationResult,
  ProviderCertificationSetup,
} from '@/lib/provider-certification';
import { cn } from '@/lib/utils';

function providerName(provider: UpstreamProvider): string {
  return provider.slug || provider.provider_type;
}

export default function MultiProviderCertificationPage() {
  const [selectedProviderIds, setSelectedProviderIds] = useState<number[]>([]);
  const [activeProviderId, setActiveProviderId] = useState<number | null>(null);
  const [setups, setSetups] = useState<
    Record<number, ProviderCertificationSetup>
  >({});
  const [workspaceTab, setWorkspaceTab] = useState<'setup' | 'results'>(
    'setup'
  );
  const [resultsByProvider, setResultsByProvider] = useState<
    Record<number, ModelCertificationResult[]>
  >({});
  const [progressByProvider, setProgressByProvider] = useState<
    Record<number, CertificationProgress | null>
  >({});
  const [runningProviderIds, setRunningProviderIds] = useState<number[]>([]);

  const providersQuery = useQuery({
    queryKey: ['upstream-providers'],
    queryFn: () => AdminService.getUpstreamProviders(),
    refetchOnWindowFocus: false,
  });
  const providers = useMemo(
    () => providersQuery.data ?? [],
    [providersQuery.data]
  );
  const providersById = useMemo(
    () => new Map(providers.map((provider) => [provider.id, provider])),
    [providers]
  );

  const modelQueries = useQueries({
    queries: selectedProviderIds.map((providerId) => ({
      queryKey: ['provider-models', providerId],
      queryFn: () => AdminService.getProviderModels(providerId),
      refetchOnWindowFocus: false,
    })),
  });
  const modelQueryByProvider = new Map(
    selectedProviderIds.map((providerId, index) => [
      providerId,
      modelQueries[index],
    ])
  );

  const selectedProviders = selectedProviderIds
    .map((providerId) => providersById.get(providerId))
    .filter((provider): provider is UpstreamProvider => Boolean(provider));
  const activeProvider = activeProviderId
    ? providersById.get(activeProviderId)
    : undefined;
  const activeSetup = activeProviderId
    ? (setups[activeProviderId] ?? emptyCertificationSetup())
    : undefined;
  const activeModelsQuery = activeProviderId
    ? modelQueryByProvider.get(activeProviderId)
    : undefined;

  useEffect(() => {
    if (runningProviderIds.length === 0) return;
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    window.addEventListener('beforeunload', warnBeforeUnload);
    return () => window.removeEventListener('beforeunload', warnBeforeUnload);
  }, [runningProviderIds.length]);

  const toggleProvider = (providerId: number) => {
    if (runningProviderIds.includes(providerId)) return;
    const selected = selectedProviderIds.includes(providerId);
    if (selected) {
      const remaining = selectedProviderIds.filter((id) => id !== providerId);
      setSelectedProviderIds(remaining);
      if (activeProviderId === providerId) {
        setActiveProviderId(remaining[0] ?? null);
      }
      return;
    }
    setSelectedProviderIds((current) => [...current, providerId]);
    setSetups((current) => ({
      ...current,
      [providerId]: current[providerId] ?? emptyCertificationSetup(),
    }));
    setActiveProviderId(providerId);
  };

  const updateProviderSetup = (
    providerId: number,
    setup: ProviderCertificationSetup
  ) => {
    setSetups((current) => ({ ...current, [providerId]: setup }));
    setResultsByProvider((current) => ({ ...current, [providerId]: [] }));
  };

  const providerRuns = (providerId: number) =>
    buildModelRuns(
      setups[providerId] ?? emptyCertificationSetup(),
      modelQueryByProvider.get(providerId)?.data as ProviderModels | undefined
    );

  const isProviderReady = (providerId: number): boolean => {
    const setup = setups[providerId] ?? emptyCertificationSetup();
    return (
      setup.selectedModelIds.length > 0 &&
      getModelsNeedingPath(setup).length === 0 &&
      Boolean(modelQueryByProvider.get(providerId)?.data)
    );
  };

  const runOneProvider = async (providerId: number) => {
    const setup = setups[providerId] ?? emptyCertificationSetup();
    const modelRuns = providerRuns(providerId);
    setResultsByProvider((current) => ({ ...current, [providerId]: [] }));
    setProgressByProvider((current) => ({ ...current, [providerId]: null }));
    setRunningProviderIds((current) =>
      current.includes(providerId) ? current : [...current, providerId]
    );
    try {
      await runProviderCertification({
        providerId,
        modelRuns,
        includeCache: setup.checkCache,
        onProgress: (progress) =>
          setProgressByProvider((current) => ({
            ...current,
            [providerId]: progress,
          })),
        onResults: (results) =>
          setResultsByProvider((current) => ({
            ...current,
            [providerId]: results,
          })),
      });
    } finally {
      setProgressByProvider((current) => ({
        ...current,
        [providerId]: null,
      }));
      setRunningProviderIds((current) =>
        current.filter((id) => id !== providerId)
      );
    }
  };

  const runAllProviders = () => {
    setWorkspaceTab('results');
    void Promise.all(selectedProviderIds.map(runOneProvider));
  };

  const activeResults = activeProviderId
    ? (resultsByProvider[activeProviderId] ?? [])
    : [];
  const activeNames = getCertificationModelNames(
    activeModelsQuery?.data as ProviderModels | undefined
  );

  const totalModels = selectedProviderIds.reduce(
    (total, providerId) =>
      total + (setups[providerId]?.selectedModelIds.length ?? 0),
    0
  );
  const totalRoutes = selectedProviderIds.reduce(
    (total, providerId) =>
      total + countCertificationTargets(providerRuns(providerId)),
    0
  );
  const incompleteProviders = selectedProviderIds.filter(
    (providerId) => !isProviderReady(providerId)
  );
  const allReady =
    selectedProviderIds.length > 0 && incompleteProviders.length === 0;
  const allResults = Object.values(resultsByProvider).flat();
  const aggregateSummary = summarizeCertificationResults(allResults);
  const pendingRoutes = Math.max(totalRoutes - allResults.length, 0);

  return (
    <AppPageShell contentClassName='mx-auto flex w-full max-w-7xl flex-col'>
      <div className='flex min-h-0 flex-1 flex-col gap-4'>
        <PageHeader
          title='Provider Certification'
          description='Configure and certify models across multiple upstream providers.'
          actions={
            <div className='flex gap-2'>
              <Button asChild variant='outline'>
                <Link href='/providers'>
                  <ArrowLeft className='h-4 w-4' />
                  Providers
                </Link>
              </Button>
              <Popover>
                <PopoverTrigger asChild>
                  <Button variant='outline'>
                    <Server className='h-4 w-4' />
                    Select providers
                    <Badge variant='secondary'>
                      {selectedProviderIds.length}
                    </Badge>
                    <ChevronDown className='h-4 w-4' />
                  </Button>
                </PopoverTrigger>
                <PopoverContent align='end' className='w-80 p-0'>
                  <Command className='h-auto'>
                    <CommandInput placeholder='Search providers…' />
                    <CommandList className='max-h-72'>
                      <CommandEmpty>
                        {providersQuery.isLoading
                          ? 'Loading providers…'
                          : 'No providers found'}
                      </CommandEmpty>
                      {providers.map((provider) => (
                        <CommandItem
                          key={provider.id}
                          value={`${providerName(provider)} ${provider.base_url}`}
                          onSelect={() => toggleProvider(provider.id)}
                          disabled={runningProviderIds.includes(provider.id)}
                        >
                          <Checkbox
                            checked={selectedProviderIds.includes(provider.id)}
                            tabIndex={-1}
                            aria-hidden='true'
                            className='pointer-events-none'
                          />
                          <div className='min-w-0 flex-1'>
                            <div className='truncate'>
                              {providerName(provider)}
                            </div>
                            <div className='text-muted-foreground truncate text-xs'>
                              {provider.base_url}
                            </div>
                          </div>
                          <Badge
                            variant={provider.enabled ? 'secondary' : 'outline'}
                            className='text-[10px]'
                          >
                            {provider.enabled ? 'Enabled' : 'Disabled'}
                          </Badge>
                        </CommandItem>
                      ))}
                    </CommandList>
                  </Command>
                </PopoverContent>
              </Popover>
            </div>
          }
        />

        {selectedProviders.length === 0 ? (
          <Card className='flex min-h-72 items-center justify-center'>
            <CardContent className='space-y-3 text-center'>
              <Server className='text-muted-foreground mx-auto h-8 w-8' />
              <div>
                <div className='font-medium'>Select providers to certify</div>
                <p className='text-muted-foreground mt-1 text-sm'>
                  Choose two or more providers to configure independent model
                  and path runs.
                </p>
              </div>
            </CardContent>
          </Card>
        ) : (
          <div className='grid min-h-0 flex-1 gap-4 md:grid-cols-[240px_minmax(0,1fr)]'>
            <aside className='hidden min-h-0 space-y-2 overflow-y-auto rounded-lg border p-2 md:block'>
              {selectedProviders.map((provider) => {
                const setup = setups[provider.id] ?? emptyCertificationSetup();
                const runs = providerRuns(provider.id);
                const results = resultsByProvider[provider.id] ?? [];
                const summary = summarizeCertificationResults(results);
                const running = runningProviderIds.includes(provider.id);
                return (
                  <button
                    key={provider.id}
                    type='button'
                    onClick={() => setActiveProviderId(provider.id)}
                    className={cn(
                      'hover:bg-muted w-full space-y-2 rounded-md border p-3 text-left transition-colors',
                      activeProviderId === provider.id &&
                        'border-primary bg-muted/60'
                    )}
                  >
                    <div className='flex items-start justify-between gap-2'>
                      <div className='min-w-0'>
                        <div className='truncate text-sm font-medium'>
                          {providerName(provider)}
                        </div>
                        <div className='text-muted-foreground truncate text-xs'>
                          {provider.base_url}
                        </div>
                      </div>
                      {running ? (
                        <Loader2 className='h-4 w-4 shrink-0 animate-spin' />
                      ) : results.length > 0 ? (
                        <CheckCircle2 className='h-4 w-4 shrink-0 text-emerald-600' />
                      ) : null}
                    </div>
                    <div className='text-muted-foreground flex flex-wrap gap-1 text-[11px]'>
                      <span>{setup.selectedModelIds.length} models</span>
                      <span>·</span>
                      <span>{countCertificationTargets(runs)} routes</span>
                    </div>
                    {results.length > 0 && (
                      <div className='flex flex-wrap gap-1'>
                        <Badge variant='secondary' className='text-[10px]'>
                          {summary.ok} ok
                        </Badge>
                        {(summary.warn > 0 ||
                          summary.fail > 0 ||
                          summary.error > 0) && (
                          <Badge variant='outline' className='text-[10px]'>
                            {summary.warn + summary.fail + summary.error} issues
                          </Badge>
                        )}
                      </div>
                    )}
                  </button>
                );
              })}
            </aside>

            <div className='min-h-0 space-y-3 md:hidden'>
              <Select
                value={activeProviderId?.toString()}
                onValueChange={(value) => setActiveProviderId(Number(value))}
              >
                <SelectTrigger className='w-full'>
                  <SelectValue placeholder='Choose active provider' />
                </SelectTrigger>
                <SelectContent>
                  {selectedProviders.map((provider) => (
                    <SelectItem
                      key={provider.id}
                      value={provider.id.toString()}
                    >
                      {providerName(provider)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {activeProvider && activeSetup ? (
              <Card className='flex min-h-[36rem] min-w-0 flex-col overflow-hidden p-0 md:min-h-0'>
                <div className='shrink-0 border-b px-4 py-3'>
                  <div className='flex flex-wrap items-start justify-between gap-2'>
                    <div className='min-w-0'>
                      <div className='font-medium'>
                        {providerName(activeProvider)}
                      </div>
                      <div className='text-muted-foreground truncate text-xs'>
                        {activeProvider.base_url}
                      </div>
                    </div>
                    <Badge
                      variant={activeProvider.enabled ? 'secondary' : 'outline'}
                    >
                      {activeProvider.enabled ? 'Enabled' : 'Disabled'}
                    </Badge>
                  </div>
                </div>

                <Tabs
                  value={workspaceTab}
                  onValueChange={(value) =>
                    setWorkspaceTab(value as 'setup' | 'results')
                  }
                  className='min-h-0 flex-1 overflow-hidden px-4 pb-4'
                >
                  <TabsList className='grid w-full shrink-0 grid-cols-2'>
                    <TabsTrigger value='setup'>Setup</TabsTrigger>
                    <TabsTrigger value='results'>
                      Results
                      {activeResults.length > 0 && (
                        <Badge
                          variant='secondary'
                          className='ml-1 px-1.5 py-0 text-xs'
                        >
                          {activeResults.length}
                        </Badge>
                      )}
                    </TabsTrigger>
                  </TabsList>

                  <TabsContent
                    value='setup'
                    className='mt-0 min-h-0 overflow-hidden data-[state=active]:flex data-[state=active]:flex-col'
                  >
                    <div className='min-h-0 flex-1 overflow-y-auto pr-1'>
                      <ProviderCertificationSetupPanel
                        models={
                          activeModelsQuery?.data as ProviderModels | undefined
                        }
                        isLoading={activeModelsQuery?.isLoading}
                        error={activeModelsQuery?.error}
                        setup={activeSetup}
                        onChange={(next) =>
                          updateProviderSetup(activeProvider.id, next)
                        }
                        disabled={runningProviderIds.includes(
                          activeProvider.id
                        )}
                        idPrefix={`multi-certify-${activeProvider.id}`}
                      />
                    </div>
                    <div className='mt-3 flex shrink-0 justify-end border-t pt-3'>
                      <Button
                        variant='outline'
                        size='sm'
                        onClick={() => {
                          setWorkspaceTab('results');
                          void runOneProvider(activeProvider.id);
                        }}
                        disabled={
                          runningProviderIds.includes(activeProvider.id) ||
                          !isProviderReady(activeProvider.id)
                        }
                      >
                        {runningProviderIds.includes(activeProvider.id) ? (
                          <Loader2 className='h-4 w-4 animate-spin' />
                        ) : (
                          <RotateCcw className='h-4 w-4' />
                        )}
                        Run this provider
                      </Button>
                    </div>
                  </TabsContent>

                  <TabsContent
                    value='results'
                    className='mt-0 min-h-0 overflow-hidden data-[state=active]:flex data-[state=active]:flex-col'
                  >
                    <div className='mb-2 flex shrink-0 justify-end'>
                      <Button
                        variant='outline'
                        size='sm'
                        onClick={() => void runOneProvider(activeProvider.id)}
                        disabled={
                          runningProviderIds.includes(activeProvider.id) ||
                          !isProviderReady(activeProvider.id)
                        }
                      >
                        {runningProviderIds.includes(activeProvider.id) ? (
                          <Loader2 className='h-4 w-4 animate-spin' />
                        ) : (
                          <RotateCcw className='h-4 w-4' />
                        )}
                        Run this provider again
                      </Button>
                    </div>
                    <ProviderCertificationResults
                      results={activeResults}
                      progress={progressByProvider[activeProvider.id]}
                      namesById={activeNames}
                      emptyMessage='Run this provider or the full batch to see results.'
                    />
                  </TabsContent>
                </Tabs>
              </Card>
            ) : null}
          </div>
        )}

        {selectedProviders.length > 0 && (
          <div className='bg-background sticky bottom-0 flex shrink-0 flex-col gap-3 rounded-lg border p-3 shadow-sm sm:flex-row sm:items-center sm:justify-between'>
            <div className='text-sm'>
              <div className='font-medium'>
                {selectedProviderIds.length} providers · {totalModels} models ·{' '}
                {totalRoutes} routes
              </div>
              <div className='text-muted-foreground text-xs'>
                {incompleteProviders.length > 0
                  ? `${incompleteProviders.length} provider${incompleteProviders.length === 1 ? '' : 's'} need a model or path selection. `
                  : 'Providers run concurrently; models are sequential and paths run in parallel. '}
                Status: {aggregateSummary.ok} ok, {aggregateSummary.warn}{' '}
                warnings, {aggregateSummary.fail} failed,{' '}
                {aggregateSummary.error} errors, {runningProviderIds.length}{' '}
                running, {pendingRoutes} pending.
              </div>
            </div>
            <Button
              onClick={runAllProviders}
              disabled={!allReady || runningProviderIds.length > 0}
            >
              {runningProviderIds.length > 0 ? (
                <Loader2 className='h-4 w-4 animate-spin' />
              ) : (
                <Play className='h-4 w-4' />
              )}
              {runningProviderIds.length > 0
                ? `Running ${runningProviderIds.length} provider${runningProviderIds.length === 1 ? '' : 's'}`
                : 'Run all providers'}
            </Button>
          </div>
        )}
      </div>
    </AppPageShell>
  );
}
