'use client';

import { ChevronDown } from 'lucide-react';

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
import { Label } from '@/components/ui/label';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';
import type { AdminModel, ProviderModels } from '@/lib/api/services/admin';
import type {
  ModelPathMode,
  ProviderCertificationSetup,
} from '@/lib/provider-certification';
import {
  emptyCertificationSetup,
  getCertificationPathLabel,
  getErrorMessage,
  getExactCertificationPaths,
  getModelsNeedingPath,
} from '@/lib/provider-certification';

interface ModelOption {
  model: AdminModel;
  source: 'configured' | 'discovered';
}

interface ProviderCertificationSetupProps {
  models?: ProviderModels;
  isLoading?: boolean;
  error?: unknown;
  setup: ProviderCertificationSetup;
  onChange: (setup: ProviderCertificationSetup) => void;
  disabled?: boolean;
  idPrefix: string;
}

export function getCertificationModelNames(
  models: ProviderModels | undefined
): Map<string, string> {
  return new Map(
    [...(models?.db_models ?? []), ...(models?.remote_models ?? [])].map(
      (model) => [model.id, model.name || model.id]
    )
  );
}

export function ProviderCertificationSetupPanel({
  models,
  isLoading = false,
  error,
  setup,
  onChange,
  disabled = false,
  idPrefix,
}: ProviderCertificationSetupProps) {
  const configuredOptions: ModelOption[] = (models?.db_models ?? []).map(
    (model) => ({ model, source: 'configured' })
  );
  const discoveredOptions: ModelOption[] = (models?.remote_models ?? []).map(
    (model) => ({ model, source: 'discovered' })
  );
  const namesById = getCertificationModelNames(models);
  const modelsNeedingPath = getModelsNeedingPath(setup);

  const toggleModel = (modelId: string) => {
    const isSelected = setup.selectedModelIds.includes(modelId);
    const pathModes = { ...setup.pathModes };
    const selectedModelPaths = { ...setup.selectedModelPaths };
    if (isSelected) {
      delete pathModes[modelId];
      delete selectedModelPaths[modelId];
    } else {
      pathModes[modelId] = 'default';
    }
    onChange({
      ...setup,
      selectedModelIds: isSelected
        ? setup.selectedModelIds.filter((id) => id !== modelId)
        : [...setup.selectedModelIds, modelId],
      pathModes,
      selectedModelPaths,
    });
  };

  const renderModelGroup = (label: string, options: ModelOption[]) => {
    if (options.length === 0) return null;
    return (
      <CommandGroup heading={`${label} (${options.length})`}>
        {options.map(({ model }) => (
          <CommandItem
            key={`${label}-${model.id}`}
            value={`${model.name} ${model.id}`}
            onSelect={() => toggleModel(model.id)}
            disabled={disabled}
          >
            <Checkbox
              checked={setup.selectedModelIds.includes(model.id)}
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
    <div className='space-y-4'>
      <div className='space-y-2'>
        <div className='flex items-center justify-between gap-3'>
          <Label>Models</Label>
          <div className='flex items-center gap-2'>
            <span className='text-muted-foreground text-xs'>
              {setup.selectedModelIds.length} selected
            </span>
            {setup.selectedModelIds.length > 0 && !disabled && (
              <Button
                type='button'
                variant='ghost'
                size='sm'
                onClick={() =>
                  onChange({
                    ...emptyCertificationSetup(),
                    checkCache: setup.checkCache,
                  })
                }
              >
                Clear
              </Button>
            )}
          </div>
        </div>
        <Command className='h-auto rounded-md border'>
          <CommandInput
            placeholder='Search models by name or ID…'
            disabled={isLoading || disabled}
          />
          <CommandList className='max-h-64'>
            <CommandEmpty>
              {isLoading ? 'Loading models…' : 'No models found'}
            </CommandEmpty>
            {renderModelGroup('Configured models', configuredOptions)}
            {renderModelGroup('Discovered models', discoveredOptions)}
          </CommandList>
        </Command>
        {Boolean(error) && (
          <p className='text-destructive text-sm'>{getErrorMessage(error)}</p>
        )}
      </div>

      {setup.selectedModelIds.map((modelId) => {
        const paths = getExactCertificationPaths(models, modelId);
        const mode = setup.pathModes[modelId] ?? 'default';
        const selectedPaths = setup.selectedModelPaths[modelId] ?? [];
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
                    onChange({
                      ...setup,
                      pathModes: {
                        ...setup.pathModes,
                        [modelId]: value as ModelPathMode,
                      },
                    });
                  }}
                  disabled={disabled}
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
                        disabled={disabled}
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
                          const checked = selectedPaths.includes(path.path);
                          return (
                            <label
                              key={path.path}
                              className='hover:bg-muted flex cursor-pointer items-center gap-2 rounded-sm px-2 py-1.5 text-sm'
                            >
                              <Checkbox
                                checked={checked}
                                onCheckedChange={(value) => {
                                  onChange({
                                    ...setup,
                                    selectedModelPaths: {
                                      ...setup.selectedModelPaths,
                                      [modelId]:
                                        value === true
                                          ? [...selectedPaths, path.path]
                                          : selectedPaths.filter(
                                              (item) => item !== path.path
                                            ),
                                    },
                                  });
                                }}
                              />
                              <span className='min-w-0 truncate'>
                                {getCertificationPathLabel(path)}
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

      <div className='flex items-center gap-2'>
        <Checkbox
          id={`${idPrefix}-cache`}
          checked={setup.checkCache}
          onCheckedChange={(value) =>
            onChange({ ...setup, checkCache: value === true })
          }
          disabled={disabled}
        />
        <Label htmlFor={`${idPrefix}-cache`} className='text-sm'>
          Probe prompt caching and margin
        </Label>
      </div>
      {setup.checkCache && (
        <p className='text-muted-foreground text-xs'>
          Sends 2–3 extra completions with a ~4.4k-token prompt per model path,
          billed by the upstream.
        </p>
      )}
    </div>
  );
}
