'use client';

import { useEffect, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Loader2,
  RotateCcw,
  XCircle,
} from 'lucide-react';

import { AdminService } from '@/lib/api/services/admin';
import type {
  AdminModel,
  CertificationPath,
  CertificationRow,
  CertificationStatus,
  ProviderCertification,
  UpstreamProvider,
} from '@/lib/api/services/admin';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from '@/components/ui/command';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Label } from '@/components/ui/label';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';
import { cn } from '@/lib/utils';

interface ProviderCertificationDialogProps {
  provider: UpstreamProvider;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

interface ModelOption {
  model: AdminModel;
  source: 'configured' | 'discovered';
}

type ModelPathMode = 'default' | 'selected' | 'all';

interface ModelPathTarget {
  path?: string;
  label: string;
}

interface ModelRun {
  modelId: string;
  targets: ModelPathTarget[];
}

interface ModelCertificationResult {
  resultKey: string;
  modelId: string;
  pathLabel: string;
  report?: ProviderCertification;
  error?: string;
}

const STATUS_STYLES: Record<
  CertificationStatus,
  { label: string; icon: typeof CheckCircle2; className: string }
> = {
  ok: {
    label: 'OK',
    icon: CheckCircle2,
    className:
      'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400',
  },
  warn: {
    label: 'Warn',
    icon: AlertTriangle,
    className:
      'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400',
  },
  fail: {
    label: 'Fail',
    icon: XCircle,
    className: 'border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-400',
  },
};

function StatusBadge({ status }: { status: CertificationStatus }) {
  const style = STATUS_STYLES[status];
  const Icon = style.icon;
  return (
    <Badge variant='outline' className={cn('gap-1', style.className)}>
      <Icon className='h-3 w-3' />
      {style.label}
    </Badge>
  );
}

function RowItem({ row }: { row: CertificationRow }) {
  const [showEvidence, setShowEvidence] = useState(false);
  const hasEvidence = Object.keys(row.evidence).length > 0;
  return (
    <li className='rounded-md border p-3 text-sm'>
      <div className='flex items-start justify-between gap-3'>
        <div className='min-w-0'>
          <div className='font-medium'>{row.title}</div>
          <div className='text-muted-foreground mt-0.5 break-words'>
            {row.detail}
          </div>
          <div className='text-muted-foreground mt-1 font-mono text-xs'>
            {row.id}
          </div>
        </div>
        <StatusBadge status={row.status} />
      </div>
      {hasEvidence && (
        <div className='mt-2'>
          <button
            type='button'
            onClick={() => setShowEvidence((value) => !value)}
            className='text-muted-foreground hover:text-foreground inline-flex items-center gap-1 text-xs'
          >
            {showEvidence ? (
              <ChevronDown className='h-3 w-3' />
            ) : (
              <ChevronRight className='h-3 w-3' />
            )}
            Evidence
          </button>
          {showEvidence && (
            <pre className='bg-muted mt-2 max-h-64 overflow-auto rounded p-2 font-mono text-xs'>
              {JSON.stringify(row.evidence, null, 2)}
            </pre>
          )}
        </div>
      )}
    </li>
  );
}

function ChecklistSummary({ report }: { report: ProviderCertification }) {
  return (
    <ul className='grid gap-2 sm:grid-cols-2'>
      {report.checklist.map((goal) => {
        const style = STATUS_STYLES[goal.status];
        const Icon = style.icon;
        return (
          <li
            key={goal.goal}
            className={cn(
              'flex items-start gap-2 rounded-md border p-2 text-sm',
              style.className
            )}
          >
            <Icon className='mt-0.5 h-4 w-4 shrink-0' />
            <span>{goal.label}</span>
          </li>
        );
      })}
    </ul>
  );
}

function CertificationReport({ report }: { report: ProviderCertification }) {
  const failing = report.rows.filter((row) => row.status === 'fail').length;
  const warning = report.rows.filter((row) => row.status === 'warn').length;

  return (
    <div className='space-y-4'>
      <ChecklistSummary report={report} />
      <div className='text-muted-foreground flex flex-wrap items-center gap-x-3 gap-y-1 text-xs'>
        <span>
          {report.rows.length} checks · {failing} failed · {warning} warnings
        </span>
        <span>Generated {new Date(report.generated_at).toLocaleString()}</span>
      </div>
      <ul className='space-y-2'>
        {report.rows.map((row) => (
          <RowItem key={row.id} row={row} />
        ))}
      </ul>
    </div>
  );
}

function getErrorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return 'Certification request failed';
}

function resultStatus(
  result: ModelCertificationResult
): CertificationStatus | 'error' {
  if (result.error || !result.report) return 'error';
  if (result.report.rows.some((row) => row.status === 'fail')) return 'fail';
  if (result.report.rows.some((row) => row.status === 'warn')) return 'warn';
  return 'ok';
}

export function ProviderCertificationDialog({
  provider,
  open,
  onOpenChange,
}: ProviderCertificationDialogProps) {
  const [checkCache, setCheckCache] = useState(true);
  const [workspaceTab, setWorkspaceTab] = useState<'setup' | 'results'>('setup');
  const [selectedModelIds, setSelectedModelIds] = useState<string[]>([]);
  const [pathModes, setPathModes] = useState<Record<string, ModelPathMode>>({});
  const [selectedModelPaths, setSelectedModelPaths] = useState<
    Record<string, string[]>
  >({});
  const [results, setResults] = useState<ModelCertificationResult[]>([]);
  const [currentModel, setCurrentModel] = useState<{
    id: string;
    index: number;
    total: number;
    pathCount: number;
  } | null>(null);

  const models = useQuery({
    queryKey: ['provider-models', provider.id],
    queryFn: () => AdminService.getProviderModels(provider.id),
    enabled: open,
  });

  const certify = useMutation({
    mutationFn: async ({
      modelRuns,
      includeCache,
    }: {
      modelRuns: ModelRun[];
      includeCache: boolean;
    }) => {
      const completed: ModelCertificationResult[] = [];
      setResults([]);
      setWorkspaceTab('results');

      for (const [index, run] of modelRuns.entries()) {
        setCurrentModel({
          id: run.modelId,
          index: index + 1,
          total: modelRuns.length,
          pathCount: run.targets.length,
        });
        const batch = await Promise.all(
          run.targets.map(async (target): Promise<ModelCertificationResult> => {
            const resultKey = `${run.modelId}::${target.path ?? 'default'}`;
            try {
              const report = await AdminService.certifyProvider(provider.id, {
                model_id: run.modelId,
                model_path: target.path,
                check_cache: includeCache,
              });
              return {
                resultKey,
                modelId: run.modelId,
                pathLabel: target.label,
                report,
              };
            } catch (error) {
              return {
                resultKey,
                modelId: run.modelId,
                pathLabel: target.label,
                error: getErrorMessage(error),
              };
            }
          })
        );
        completed.push(...batch);
        setResults([...completed]);
      }

      return completed;
    },
    onSettled: () => setCurrentModel(null),
  });
  const { reset: resetCertification } = certify;

  useEffect(() => {
    if (!open) {
      resetCertification();
      setWorkspaceTab('setup');
      setSelectedModelIds([]);
      setPathModes({});
      setSelectedModelPaths({});
      setResults([]);
      setCurrentModel(null);
    }
  }, [open, resetCertification]);

  const configuredOptions: ModelOption[] =
    models.data?.db_models.map((model) => ({
      model,
      source: 'configured',
    })) ?? [];
  const discoveredOptions: ModelOption[] =
    models.data?.remote_models.map((model) => ({
      model,
      source: 'discovered',
    })) ?? [];
  const allOptions = [...configuredOptions, ...discoveredOptions];
  const namesById = new Map(
    allOptions.map(({ model }) => [model.id, model.name || model.id])
  );

  const pathsForModel = (modelId: string): CertificationPath[] =>
    (models.data?.certification_paths[modelId] ?? []).filter(
      (path) => path.endpoint_tag
    );

  const pathLabel = (path: CertificationPath): string =>
    path.endpoint_name && path.endpoint_name !== path.endpoint_tag
      ? `${path.endpoint_name} (${path.endpoint_tag})`
      : path.endpoint_tag || 'Provider default';

  const toggleModel = (modelId: string) => {
    const isSelected = selectedModelIds.includes(modelId);
    setSelectedModelIds((current) =>
      isSelected
        ? current.filter((id) => id !== modelId)
        : [...current, modelId]
    );
    setPathModes((current) => {
      const next = { ...current };
      if (isSelected) delete next[modelId];
      else next[modelId] = 'default';
      return next;
    });
    setSelectedModelPaths((current) => {
      const next = { ...current };
      if (isSelected) delete next[modelId];
      return next;
    });
    resetCertification();
    setResults([]);
  };

  const modelsNeedingPath = selectedModelIds.filter(
    (modelId) =>
      pathModes[modelId] === 'selected' &&
      (selectedModelPaths[modelId]?.length ?? 0) === 0
  );

  const buildModelRuns = (): ModelRun[] =>
    selectedModelIds.map((modelId) => {
      const paths = pathsForModel(modelId);
      const mode = pathModes[modelId] ?? 'default';
      if (mode === 'all') {
        return {
          modelId,
          targets: paths.map((path) => ({
            path: path.path,
            label: pathLabel(path),
          })),
        };
      }
      if (mode === 'selected') {
        const selected = new Set(selectedModelPaths[modelId] ?? []);
        return {
          modelId,
          targets: paths
            .filter((path) => selected.has(path.path))
            .map((path) => ({ path: path.path, label: pathLabel(path) })),
        };
      }
      return { modelId, targets: [{ label: 'Provider default' }] };
    });

  const modelRuns = buildModelRuns();
  const targetCount = modelRuns.reduce(
    (total, run) => total + run.targets.length,
    0
  );

  const renderModelGroup = (label: string, options: ModelOption[]) => {
    if (options.length === 0) return null;
    return (
      <CommandGroup heading={`${label} (${options.length})`}>
        {options.map(({ model }) => (
          <CommandItem
            key={`${label}-${model.id}`}
            value={`${model.name} ${model.id}`}
            onSelect={() => toggleModel(model.id)}
            disabled={certify.isPending}
          >
            <Checkbox
              checked={selectedModelIds.includes(model.id)}
              tabIndex={-1}
              aria-hidden='true'
              className='pointer-events-none'
            />
            <span className='min-w-0 flex-1 truncate'>
              {model.name || model.id}
            </span>
            <span className='text-muted-foreground max-w-56 truncate font-mono text-xs'>
              {model.id}
            </span>
          </CommandItem>
        ))}
      </CommandGroup>
    );
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className='flex h-[90dvh] max-h-[90dvh] flex-col overflow-hidden sm:max-w-[780px]'>
        <DialogHeader className='shrink-0'>
          <DialogTitle>Certify upstream models</DialogTitle>
          <DialogDescription>
            Select models, then use the provider default, choose specific
            paths, or test every path. Models run one at a time; paths for the
            same model run in parallel against {provider.base_url}.
          </DialogDescription>
        </DialogHeader>

        <Tabs
          value={workspaceTab}
          onValueChange={(value) =>
            setWorkspaceTab(value as 'setup' | 'results')
          }
          className='min-h-0 flex-1 overflow-hidden'
        >
          <TabsList className='grid w-full shrink-0 grid-cols-2'>
            <TabsTrigger value='setup'>Setup</TabsTrigger>
            <TabsTrigger
              value='results'
              disabled={!certify.isPending && results.length === 0}
            >
              Results
              {(certify.isPending || results.length > 0) && (
                <Badge variant='secondary' className='ml-1 px-1.5 py-0 text-xs'>
                  {results.length}/{targetCount}
                </Badge>
              )}
            </TabsTrigger>
          </TabsList>

          <TabsContent
            value='setup'
            className='mt-0 min-h-0 overflow-hidden data-[state=active]:flex data-[state=active]:flex-col'
          >
            <div className='min-h-0 flex-1 space-y-4 overflow-y-auto pr-1'>
              <div className='space-y-2'>
                <div className='flex items-center justify-between gap-3'>
                  <Label>Models</Label>
                  <div className='flex items-center gap-2'>
                    <span className='text-muted-foreground text-xs'>
                      {selectedModelIds.length} selected
                    </span>
                    {selectedModelIds.length > 0 && !certify.isPending && (
                      <Button
                        type='button'
                        variant='ghost'
                        size='sm'
                        onClick={() => {
                          setSelectedModelIds([]);
                          setPathModes({});
                          setSelectedModelPaths({});
                          setResults([]);
                          resetCertification();
                        }}
                      >
                        Clear
                      </Button>
                    )}
                  </div>
                </div>
                <Command className='h-auto rounded-md border'>
                  <CommandInput
                    placeholder='Search models by name or ID…'
                    disabled={models.isLoading || certify.isPending}
                  />
                  <CommandList className='max-h-64'>
                    <CommandEmpty>
                      {models.isLoading ? 'Loading models…' : 'No models found'}
                    </CommandEmpty>
                    {renderModelGroup('Configured models', configuredOptions)}
                    {renderModelGroup('Discovered models', discoveredOptions)}
                  </CommandList>
                </Command>
                {models.isError && (
                  <p className='text-destructive text-sm'>
                    {getErrorMessage(models.error)}
                  </p>
                )}
              </div>

              {selectedModelIds.map((modelId) => {
                const paths = pathsForModel(modelId);
                const mode = pathModes[modelId] ?? 'default';
                const selectedPaths = selectedModelPaths[modelId] ?? [];
                return (
                  <div key={modelId} className='space-y-3 rounded-md border p-3'>
                    <div className='min-w-0'>
                      <div className='truncate text-sm font-medium'>
                        {namesById.get(modelId) ?? modelId}
                      </div>
                      <div className='text-muted-foreground truncate font-mono text-xs'>
                        {modelId}
                      </div>
                    </div>
                    {paths.length > 0 ? (
                      <>
                        <ToggleGroup
                          type='single'
                          variant='outline'
                          size='sm'
                          value={mode}
                          onValueChange={(value) => {
                            if (!value) return;
                            setPathModes((current) => ({
                              ...current,
                              [modelId]: value as ModelPathMode,
                            }));
                          }}
                          disabled={certify.isPending}
                          className='w-full justify-start'
                        >
                          <ToggleGroupItem value='default'>Default</ToggleGroupItem>
                          <ToggleGroupItem value='selected'>
                            Choose paths
                          </ToggleGroupItem>
                          <ToggleGroupItem value='all'>All paths</ToggleGroupItem>
                        </ToggleGroup>
                        {mode === 'default' && (
                          <p className='text-muted-foreground text-xs'>
                            Uses the upstream provider&apos;s normal model routing.
                          </p>
                        )}
                        {mode === 'selected' && (
                          <Popover>
                            <PopoverTrigger asChild>
                              <Button
                                type='button'
                                variant='outline'
                                size='sm'
                                className='w-full justify-between'
                                disabled={certify.isPending}
                              >
                                <span className='truncate'>
                                  {selectedPaths.length === 0
                                    ? 'Select paths'
                                    : `${selectedPaths.length} path${selectedPaths.length === 1 ? '' : 's'} selected`}
                                </span>
                                <ChevronDown className='h-4 w-4' />
                              </Button>
                            </PopoverTrigger>
                            <PopoverContent
                              align='start'
                              className='w-80 max-w-[calc(100vw-2rem)] p-2'
                            >
                              <div className='max-h-64 space-y-1 overflow-y-auto overscroll-contain'>
                                {paths.map((path) => {
                                  const checked = selectedPaths.includes(
                                    path.path
                                  );
                                  return (
                                    <label
                                      key={path.path}
                                      className='hover:bg-muted flex cursor-pointer items-center gap-2 rounded-sm px-2 py-1.5 text-sm'
                                    >
                                      <Checkbox
                                        checked={checked}
                                        onCheckedChange={(value) =>
                                          setSelectedModelPaths((current) => {
                                            const previous =
                                              current[modelId] ?? [];
                                            return {
                                              ...current,
                                              [modelId]:
                                                value === true
                                                  ? [...previous, path.path]
                                                  : previous.filter(
                                                      (item) =>
                                                        item !== path.path
                                                    ),
                                            };
                                          })
                                        }
                                      />
                                      <span className='min-w-0 truncate'>
                                        {pathLabel(path)}
                                      </span>
                                    </label>
                                  );
                                })}
                              </div>
                            </PopoverContent>
                          </Popover>
                        )}
                        {mode === 'all' && (
                          <p className='text-muted-foreground text-xs'>
                            All {paths.length} paths will run in parallel.
                          </p>
                        )}
                      </>
                    ) : (
                      <p className='text-muted-foreground text-xs'>
                        Only the provider default route is available.
                      </p>
                    )}
                  </div>
                );
              })}
              {modelsNeedingPath.length > 0 && (
                <p className='text-muted-foreground text-xs'>
                  Choose at least one path for each model using “Choose paths”.
                </p>
              )}
            </div>

            <div className='mt-3 flex shrink-0 flex-wrap items-center justify-between gap-3 border-t pt-3'>
              <div className='flex items-center gap-2'>
                <Checkbox
                  id={`certify-cache-${provider.id}`}
                  checked={checkCache}
                  onCheckedChange={(value) => setCheckCache(value === true)}
                  disabled={certify.isPending}
                />
                <Label
                  htmlFor={`certify-cache-${provider.id}`}
                  className='text-sm'
                >
                  Probe prompt caching and margin
                </Label>
              </div>
              <Button
                variant='outline'
                size='sm'
                onClick={() =>
                  certify.mutate({
                    modelRuns,
                    includeCache: checkCache,
                  })
                }
                disabled={
                  certify.isPending ||
                  selectedModelIds.length === 0 ||
                  modelsNeedingPath.length > 0
                }
                className='gap-1.5'
              >
                {certify.isPending ? (
                  <Loader2 className='h-4 w-4 animate-spin' />
                ) : (
                  <RotateCcw className='h-4 w-4' />
                )}
                {certify.isPending
                  ? `Running ${currentModel?.index ?? 1} of ${currentModel?.total ?? selectedModelIds.length}`
                  : results.length > 0
                    ? `Run ${targetCount} route${targetCount === 1 ? '' : 's'} again`
                    : `Certify ${targetCount || ''} route${targetCount === 1 ? '' : 's'}`}
              </Button>
            </div>
          </TabsContent>

          <TabsContent
            value='results'
            className='mt-0 min-h-0 overflow-hidden data-[state=active]:flex data-[state=active]:flex-col'
          >
            {currentModel && (
              <div className='text-muted-foreground flex shrink-0 items-center gap-2 rounded-md border p-3 text-sm'>
                <Loader2 className='h-4 w-4 animate-spin' />
                <span className='min-w-0 truncate'>
                  Probing {namesById.get(currentModel.id) ?? currentModel.id}
                  {currentModel.pathCount > 1
                    ? ` across ${currentModel.pathCount} paths in parallel`
                    : ''}{' '}
                  — model {currentModel.index} of {currentModel.total}
                </span>
              </div>
            )}

            {results.length === 0 ? (
              <div className='text-muted-foreground flex min-h-0 flex-1 items-center justify-center text-center text-sm'>
                Results will appear here as certification completes.
              </div>
            ) : (
              <Tabs
                key={results.map((result) => result.resultKey).join('|')}
                defaultValue={results[0].resultKey}
                className='min-h-0 flex-1 overflow-hidden'
              >
                <div className='max-h-28 shrink-0 overflow-y-auto rounded-md border p-2'>
                  <TabsList className='flex h-auto w-full flex-wrap justify-start gap-1 bg-transparent p-0'>
                    {results.map((result, index) => {
                      const status = resultStatus(result);
                      const Icon =
                        status === 'error'
                          ? XCircle
                          : STATUS_STYLES[status].icon;
                      const modelRouteNumber = results
                        .slice(0, index + 1)
                        .filter((item) => item.modelId === result.modelId).length;
                      const modelRouteCount = results.filter(
                        (item) => item.modelId === result.modelId
                      ).length;
                      return (
                        <TabsTrigger
                          key={result.resultKey}
                          value={result.resultKey}
                          title={namesById.get(result.modelId) ?? result.modelId}
                          className='h-7 min-w-0 max-w-44 gap-1 px-2 text-xs'
                        >
                          <Icon
                            className={cn(
                              status === 'ok' && 'text-emerald-600',
                              status === 'warn' && 'text-amber-600',
                              (status === 'fail' || status === 'error') &&
                                'text-red-600'
                            )}
                          />
                          <span className='truncate'>
                            {namesById.get(result.modelId) ?? result.modelId}
                          </span>
                          {modelRouteCount > 1 && (
                            <span className='text-muted-foreground'>
                              {modelRouteNumber}
                            </span>
                          )}
                        </TabsTrigger>
                      );
                    })}
                  </TabsList>
                </div>

                <div className='min-h-0 flex-1 overflow-y-auto pr-1'>
                  {results.map((result) => {
                    const status = resultStatus(result);
                    return (
                      <TabsContent
                        key={result.resultKey}
                        value={result.resultKey}
                        className='space-y-3'
                      >
                        <div className='bg-muted/30 space-y-2 rounded-md border p-3'>
                          <div className='flex flex-wrap items-center justify-between gap-2'>
                            <div className='font-medium'>
                              {namesById.get(result.modelId) ?? result.modelId}
                            </div>
                            {status === 'error' ? (
                              <Badge
                                variant='outline'
                                className='border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-400'
                              >
                                Error
                              </Badge>
                            ) : (
                              <StatusBadge status={status} />
                            )}
                          </div>
                          <div className='grid gap-1 text-xs'>
                            <span className='text-muted-foreground'>
                              Model path
                            </span>
                            <div className='rounded bg-background px-2 py-1.5 font-medium'>
                              {result.pathLabel}
                            </div>
                          </div>
                        </div>
                        {result.report ? (
                          <CertificationReport report={result.report} />
                        ) : (
                          <div className='rounded-md border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-700 dark:text-red-400'>
                            {result.error ?? 'Certification failed'}
                          </div>
                        )}
                      </TabsContent>
                    );
                  })}
                </div>
              </Tabs>
            )}
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}
