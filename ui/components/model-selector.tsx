'use client';

import React, { useState, useMemo } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { type Model, type GroupSettings } from '@/lib/api/schemas/models';
import {
  AdminService,
  type AdminModelGroup,
  type AdminModel,
} from '@/lib/api/services/admin';
type ModelGroup = AdminModelGroup;
import { AddProviderModelDialog } from '@/components/add-provider-model-dialog';
import { EditGroupForm } from '@/components/edit-group-form';
import { ModelProviderSection } from '@/components/model-provider-section';
import { useDisplayCurrency } from '@/lib/hooks/use-display-currency';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Skeleton } from '@/components/ui/skeleton';
import { Alert, AlertDescription } from '@/components/ui/alert';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Trash2, Ban, CheckCircle, Plus } from 'lucide-react';
import { toast } from 'sonner';
import {
  sortModels,
  groupAndSortModelsByProvider,
} from '@/lib/utils/model-sort';

interface ModelSelectorProps {
  filterProvider?: string;
  groupData?: ModelGroup;
  filteredModels?: Model[];
  showDeleteAllButton?: boolean;
}

const modelQueryKeys = [
  ['models-with-providers'],
  ['all-provider-models'],
  ['upstream-providers'],
] as const;

const toAdminModelFromModel = (
  model: Model,
  providerId: number,
  enabled: boolean
): AdminModel => {
  const createdAt = Date.parse(model.createdAt);
  const created = Number.isNaN(createdAt)
    ? Math.floor(Date.now() / 1000)
    : Math.floor(createdAt / 1000);

  return {
    id: model.id,
    name: model.name,
    description: model.description || '',
    created,
    context_length: model.contextLength || 4096,
    architecture: {
      modality: model.modelType || 'text',
      input_modalities: [model.modelType || 'text'],
      output_modalities: [model.modelType || 'text'],
      tokenizer: '',
      instruct_type: null,
    },
    pricing: {
      prompt: model.input_cost,
      completion: model.output_cost,
      request: model.min_cost_per_request,
      image: 0,
      web_search: 0,
      internal_reasoning: 0,
    },
    per_request_limits: null,
    top_provider: null,
    upstream_provider_id: providerId,
    enabled,
    alias_ids: model.alias_ids || null,
  };
};

const groupModelsByProviderId = (selectedModelsList: Model[]) =>
  selectedModelsList.reduce<Record<string, Model[]>>((acc, model) => {
    const providerId = model.provider_id || 'unknown';
    if (!acc[providerId]) {
      acc[providerId] = [];
    }
    acc[providerId].push(model);
    return acc;
  }, {});

const isOverrideModel = (model: Model): boolean =>
  model.api_key_type !== 'remote' && Boolean(model.provider_id);

export function ModelSelector({
  filterProvider,
  groupData,
  filteredModels: propFilteredModels,
  showDeleteAllButton = false,
}: ModelSelectorProps) {
  const { displayUnit, usdPerSat } = useDisplayCurrency();
  const [, setHoveredModelId] = useState<string | null>(null);
  const [editingGroup, setEditingGroup] = useState<{
    provider: string;
    models: Model[];
    groupData: ModelGroup;
  } | null>(null);
  const [modelDialogState, setModelDialogState] = useState<{
    isOpen: boolean;
    providerId: number | null;
    mode: 'create' | 'edit' | 'override';
    initialData?: AdminModel | null;
  }>({
    isOpen: false,
    providerId: null,
    mode: 'create',
    initialData: null,
  });

  // Bulk selection state
  const [selectedModels, setSelectedModels] = useState<Set<string>>(new Set());
  const [bulkDeleteDialogOpen, setBulkDeleteDialogOpen] = useState(false);
  const [deleteAllDialogOpen, setDeleteAllDialogOpen] = useState(false);

  const queryClient = useQueryClient();

  // Fetch models and groups
  const {
    data: modelsData,
    isLoading: isLoadingModels,
    error: modelsError,
    refetch: refetchModels,
  } = useQuery({
    queryKey: ['models-with-providers'],
    queryFn: () => AdminService.getModelsWithProviders(),
    refetchOnWindowFocus: false,
  });

  const { models = [], groups = [] } = modelsData || {};
  const allOverrideModels = useMemo(
    () => models.filter(isOverrideModel),
    [models]
  );

  // Filter models by provider if specified
  const providerFilteredModels = useMemo(() => {
    if (!filterProvider) return models;
    return models.filter((model) => model.provider === filterProvider);
  }, [models, filterProvider]);

  const visibleModels = useMemo(
    () =>
      propFilteredModels !== undefined
        ? propFilteredModels
        : providerFilteredModels,
    [propFilteredModels, providerFilteredModels]
  );

  React.useEffect(() => {
    setSelectedModels((current) => {
      if (current.size === 0) {
        return current;
      }

      const visibleIds = new Set(visibleModels.map((model) => model.id));
      const next = new Set(
        Array.from(current).filter((modelId) => visibleIds.has(modelId))
      );

      return next.size === current.size ? current : next;
    });
  }, [visibleModels]);

  const invalidateModelCaches = () => {
    modelQueryKeys.forEach((queryKey) => {
      queryClient.invalidateQueries({ queryKey });
    });
  };

  // Bulk deletion mutations
  const bulkDeleteMutation = useMutation({
    mutationFn: async (modelIds: string[]) => {
      const selectedModelsList = models.filter((m) => modelIds.includes(m.id));
      const modelsByProvider = groupModelsByProviderId(selectedModelsList);

      let totalDeleted = 0;
      for (const [providerId, providerModels] of Object.entries(
        modelsByProvider
      )) {
        if (providerId === 'unknown') {
          continue;
        }
        const modelFullNames = providerModels.map((m) => m.id);
        const result = await AdminService.deleteModels(
          modelFullNames,
          providerId
        );
        totalDeleted += result.deleted_count;
      }

      return { deleted_count: totalDeleted, message: 'Models deleted' };
    },
    onSuccess: (data) => {
      toast.success(
        `Successfully deleted ${data.deleted_count} model overrides`
      );
      setSelectedModels(new Set());
      invalidateModelCaches();
    },
    onError: (error) => {
      toast.error(`Failed to delete model overrides: ${error.message}`);
    },
  });

  // Bulk soft deletion mutations (disable models)
  const bulkSoftDeleteMutation = useMutation({
    mutationFn: async (modelIds: string[]) => {
      const selectedModelsList = models.filter((m) => modelIds.includes(m.id));
      const modelsByProvider = groupModelsByProviderId(selectedModelsList);

      let totalDisabled = 0;
      for (const [providerId, providerModels] of Object.entries(
        modelsByProvider
      )) {
        if (providerId === 'unknown') {
          continue;
        }

        for (const model of providerModels) {
          const providerIdNum = parseInt(providerId);
          try {
            const existingModel = await AdminService.getProviderModel(
              providerIdNum,
              model.id
            );
            await AdminService.updateProviderModel(providerIdNum, model.id, {
              ...existingModel,
              enabled: false,
            });
            totalDisabled++;
          } catch (fetchError: unknown) {
            const error = fetchError as { message?: string; status?: number };
            if (error.message?.includes('404') || error.status === 404) {
              const newOverride = toAdminModelFromModel(
                model,
                providerIdNum,
                false
              );
              await AdminService.createProviderModel(
                providerIdNum,
                newOverride
              );
              totalDisabled++;
            }
          }
        }
      }

      return { deleted_count: totalDisabled, message: 'Models disabled' };
    },
    onSuccess: (data) => {
      toast.success(`Successfully disabled ${data.deleted_count} models`);
      setSelectedModels(new Set());
      invalidateModelCaches();
    },
    onError: (error) => {
      toast.error(`Failed to disable models: ${error.message}`);
    },
  });

  const deleteAllOverridesMutation = useMutation({
    mutationFn: async () => {
      const modelsByProvider = groupModelsByProviderId(allOverrideModels);
      let totalDeleted = 0;

      for (const [providerId, providerModels] of Object.entries(
        modelsByProvider
      )) {
        if (providerId === 'unknown' || providerModels.length === 0) {
          continue;
        }

        const result = await AdminService.deleteModels(
          providerModels.map((model) => model.id),
          providerId
        );
        totalDeleted += result.deleted_count;
      }

      return {
        deleted_count: totalDeleted,
        message: 'Model overrides deleted',
      };
    },
    onSuccess: (data) => {
      toast.success(
        `Successfully permanently deleted ${data.deleted_count} model overrides`
      );
      setSelectedModels(new Set());
      invalidateModelCaches();
    },
    onError: (error) => {
      toast.error(
        `Failed to permanently delete all model overrides: ${error.message}`
      );
    },
  });

  const deleteOverridesByProviderMutation = useMutation({
    mutationFn: async ({
      providerId,
      modelIds,
    }: {
      providerId: string;
      modelIds: string[];
    }) => {
      if (modelIds.length === 0) {
        return {
          deleted_count: 0,
          message: 'No model overrides to delete',
        };
      }
      return AdminService.deleteModels(modelIds, providerId);
    },
    onSuccess: (data) => {
      toast.success(
        `Successfully permanently deleted ${data.deleted_count} model overrides from provider`
      );
      setSelectedModels(new Set());
      invalidateModelCaches();
    },
    onError: (error) => {
      toast.error(
        `Failed to permanently delete provider overrides: ${error.message}`
      );
    },
  });

  // Restore model mutation (enable model)
  const restoreMutation = useMutation({
    mutationFn: async (modelId: string) => {
      const model = models.find((m) => m.id === modelId);
      if (!model) {
        throw new Error('Model not found');
      }

      if (!model.provider_id) {
        throw new Error('Provider ID not available for this model');
      }

      const providerId = parseInt(model.provider_id);
      const existingModel = await AdminService.getProviderModel(
        providerId,
        model.id
      );

      await AdminService.updateProviderModel(providerId, model.id, {
        ...existingModel,
        enabled: true,
      });

      return { restored_count: 1 };
    },
    onSuccess: () => {
      toast.success('Model enabled successfully');
      invalidateModelCaches();
    },
    onError: (error) => {
      toast.error(`Failed to enable model: ${error.message}`);
    },
  });

  // Bulk restore mutation (enable models)
  const bulkRestoreMutation = useMutation({
    mutationFn: async (modelIds: string[]) => {
      const selectedModelsList = models.filter((m) => modelIds.includes(m.id));
      const modelsByProvider = groupModelsByProviderId(selectedModelsList);

      let totalEnabled = 0;
      for (const [providerId, providerModels] of Object.entries(
        modelsByProvider
      )) {
        if (providerId === 'unknown') {
          continue;
        }

        for (const model of providerModels) {
          const providerIdNum = parseInt(providerId);
          try {
            const existingModel = await AdminService.getProviderModel(
              providerIdNum,
              model.id
            );
            await AdminService.updateProviderModel(providerIdNum, model.id, {
              ...existingModel,
              enabled: true,
            });
            totalEnabled++;
          } catch {
            toast.error(`Failed to enable model ${model.full_name}`);
          }
        }
      }

      return { restored_count: totalEnabled, message: 'Models enabled' };
    },
    onSuccess: (data) => {
      toast.success(`Successfully enabled ${data.restored_count} models`);
      setSelectedModels(new Set());
      invalidateModelCaches();
    },
    onError: (error) => {
      toast.error(`Failed to enable models: ${error.message}`);
    },
  });

  // Group models by provider for better organization (only if not filtering)
  const groupedModels = useMemo(() => {
    if (filterProvider) {
      const sortedModels = sortModels(visibleModels);
      return sortedModels.length > 0 ? { [filterProvider]: sortedModels } : {};
    }

    if (!visibleModels) return {};

    return groupAndSortModelsByProvider(visibleModels);
  }, [visibleModels, filterProvider]);

  // Create a map of provider names to group data
  const groupDataMap = useMemo(() => {
    return new Map(groups.map((group) => [group.provider, group]));
  }, [groups]);

  // Group settings for compatibility
  const groupSettings = useMemo(() => {
    const settings: Record<
      string,
      { group_api_key?: string; group_url?: string }
    > = {};
    groups.forEach((group) => {
      settings[group.provider] = {
        group_api_key: group.group_api_key,
        group_url: group.group_url,
      };
    });
    return settings;
  }, [groups]);

  // Utility function to get effective API key for a model
  const getEffectiveApiKey = (model: Model): string | undefined => {
    if (model.api_key) {
      return model.api_key; // Individual key takes precedence
    }
    return groupSettings[model.provider]?.group_api_key; // Fallback to group key
  };

  // Utility function to check if model has truly individual settings different from group
  const hasIndividualSettings = (model: Model): boolean => {
    const groupData = groupDataMap.get(model.provider);

    // Check if API key is different from group
    const hasIndividualApiKey = !!(
      model.api_key &&
      typeof model.api_key === 'string' &&
      model.api_key !== groupData?.group_api_key
    );

    // Check if URL is different from group (excluding relative paths like "/v1/chat/completions")
    const hasIndividualUrl = !!(
      model.url &&
      typeof model.url === 'string' &&
      !model.url.startsWith('/') &&
      model.url !== groupData?.group_url
    );

    return hasIndividualApiKey || hasIndividualUrl;
  };

  const handleAddModelClick = (providerId: number) => {
    setModelDialogState({
      isOpen: true,
      providerId,
      mode: 'create',
      initialData: null,
    });
  };

  const handleEditModelClick = (model: Model) => {
    if (!model.provider_id) {
      toast.error('Provider ID missing for model');
      return;
    }
    const providerId = parseInt(model.provider_id);

    const adminModel = toAdminModelFromModel(
      model,
      providerId,
      model.isEnabled
    );

    setModelDialogState({
      isOpen: true,
      providerId,
      mode: 'edit',
      initialData: adminModel,
    });
  };

  const handleOverrideModelClick = (model: Model) => {
    if (!model.provider_id) {
      toast.error('Provider ID missing for model');
      return;
    }
    const providerId = parseInt(model.provider_id);

    const adminModel = toAdminModelFromModel(
      model,
      providerId,
      model.isEnabled
    );

    setModelDialogState({
      isOpen: true,
      providerId,
      mode: 'override',
      initialData: adminModel,
    });
  };

  // Handle model update
  const handleModelUpdate = async () => {
    await refetchModels();
  };

  // Handle group update
  const handleGroupUpdate = async (
    oldProvider: string,
    updatedData: GroupSettings
  ) => {
    try {
      const groupData = groupDataMap.get(oldProvider);
      if (groupData) {
        await AdminService.updateModelGroup(groupData.id, {
          provider: updatedData.provider,
          group_api_key: updatedData.group_api_key || undefined,
          group_url: updatedData.group_url || undefined,
        });
        await refetchModels();
        setEditingGroup(null);
        toast.success('Group updated successfully!');
      }
    } catch {
      toast.error('Failed to update group. Please try again.');
    }
  };

  // Handle provider-specific refresh using group credentials
  const handleProviderRefresh = async (provider: string) => {
    const groupData = groupDataMap.get(provider);
    if (!groupData?.id) {
      toast.error('No group configuration found for this provider');
      return;
    }

    try {
      const response = await AdminService.refreshModels({
        provider_id: groupData.id,
      });

      toast.info(response.message);
      await refetchModels();
      invalidateModelCaches();
    } catch (error: unknown) {
      const errorMessage =
        error instanceof Error ? error.message : 'Unknown error';
      toast.error(`Failed to refresh ${provider} models: ${errorMessage}`);
    }
  };

  // Bulk selection utilities
  const toggleModelSelection = (modelId: string) => {
    const newSelected = new Set(selectedModels);
    if (newSelected.has(modelId)) {
      newSelected.delete(modelId);
    } else {
      newSelected.add(modelId);
    }
    setSelectedModels(newSelected);
  };

  const selectAllModels = () => {
    setSelectedModels(new Set(visibleModels.map((model) => model.id)));
  };

  const deselectAllModels = () => {
    setSelectedModels(new Set());
  };

  const allVisibleModelsSelected =
    visibleModels.length > 0 &&
    visibleModels.every((model) => selectedModels.has(model.id));
  const someVisibleModelsSelected =
    !allVisibleModelsSelected && selectedModels.size > 0;
  const selectedSoftDeletedModelIds = visibleModels
    .filter((model) => selectedModels.has(model.id) && model.soft_deleted)
    .map((model) => model.id);

  const selectProviderModels = (provider: string) => {
    const providerModelIds = visibleModels
      .filter((m) => m.provider === provider)
      .map((m) => m.id);
    const newSelected = new Set(selectedModels);
    providerModelIds.forEach((id) => newSelected.add(id));
    setSelectedModels(newSelected);
  };

  // Bulk operation handlers
  const handleBulkDelete = () => {
    setBulkDeleteDialogOpen(true);
  };

  const confirmBulkDelete = () => {
    bulkDeleteMutation.mutate(Array.from(selectedModels));
    setBulkDeleteDialogOpen(false);
  };

  const handleBulkSoftDelete = () => {
    bulkSoftDeleteMutation.mutate(Array.from(selectedModels));
  };

  const handleDeleteAll = () => {
    if (allOverrideModels.length === 0) {
      toast.info('No model overrides to delete');
      return;
    }
    setDeleteAllDialogOpen(true);
  };

  const confirmDeleteAll = () => {
    deleteAllOverridesMutation.mutate();
    setDeleteAllDialogOpen(false);
  };

  const handleDeleteByProvider = (provider: string) => {
    const group = groupDataMap.get(provider);
    if (!group?.id) {
      toast.error('No provider configuration found');
      return;
    }

    const providerOverrideIds = models
      .filter(
        (model) => model.provider_id === group.id && isOverrideModel(model)
      )
      .map((model) => model.id);

    if (providerOverrideIds.length === 0) {
      toast.info('No model overrides to delete for this provider');
      return;
    }

    deleteOverridesByProviderMutation.mutate({
      providerId: group.id,
      modelIds: providerOverrideIds,
    });
  };

  // Individual model deletion handler
  const handleDeleteModel = async (modelId: string) => {
    const model = models.find((m) => m.id === modelId);
    if (!model) {
      toast.error('Model not found');
      return;
    }

    if (!model.provider_id) {
      toast.error('Provider ID not available for this model');
      return;
    }

    try {
      const providerId = parseInt(model.provider_id);
      await AdminService.deleteProviderModel(providerId, model.id);
      toast.success('Model override deleted successfully');
      invalidateModelCaches();
    } catch (error: unknown) {
      const errorMessage =
        error instanceof Error ? error.message : 'Unknown error';
      toast.error(`Failed to delete model override: ${errorMessage}`);
    }
  };

  // Individual model soft deletion handler (disable model)
  const handleSoftDeleteModel = async (modelId: string) => {
    const model = models.find((m) => m.id === modelId);
    if (!model) {
      toast.error('Model not found');
      return;
    }

    if (!model.provider_id) {
      toast.error('Provider ID not available for this model');
      return;
    }

    try {
      const providerId = parseInt(model.provider_id);

      try {
        const existingModel = await AdminService.getProviderModel(
          providerId,
          model.id
        );

        await AdminService.updateProviderModel(providerId, model.id, {
          ...existingModel,
          enabled: false,
        });
        toast.success('Model disabled successfully');
      } catch (fetchError: unknown) {
        const error = fetchError as { message?: string; status?: number };
        if (error.message?.includes('404') || error.status === 404) {
          const newOverride = toAdminModelFromModel(model, providerId, false);

          await AdminService.createProviderModel(providerId, newOverride);
          toast.success('Model disabled successfully');
        } else {
          throw fetchError;
        }
      }

      invalidateModelCaches();
    } catch (error: unknown) {
      const errorMessage =
        error instanceof Error ? error.message : 'Unknown error';
      toast.error(`Failed to disable model: ${errorMessage}`);
    }
  };

  if (isLoadingModels) {
    return (
      <div className='grid gap-4'>
        <Skeleton className='h-[200px]' />
        <Skeleton className='h-[200px]' />
      </div>
    );
  }

  if (modelsError) {
    return (
      <Alert variant='destructive'>
        <AlertDescription>
          Error loading models. Please try again later.
        </AlertDescription>
      </Alert>
    );
  }

  return (
    <div className='grid gap-3 sm:gap-4'>
      <div className='flex flex-col gap-2.5'>
        <div className='flex flex-col gap-2.5 xl:flex-row xl:items-center xl:justify-between'>
          <div className='flex flex-wrap items-center gap-1'>
            <label className='inline-flex h-7 cursor-pointer items-center gap-2 px-1 text-xs font-medium select-none sm:h-8 sm:px-1.5 sm:text-sm'>
              <Checkbox
                checked={
                  allVisibleModelsSelected
                    ? true
                    : someVisibleModelsSelected
                      ? 'indeterminate'
                      : false
                }
                onCheckedChange={(checked) => {
                  if (checked === true) {
                    selectAllModels();
                    return;
                  }

                  deselectAllModels();
                }}
                className='border-border/90 data-checked:ring-primary/35 size-5 data-checked:ring-2'
                aria-label='Select all visible models'
              />
              <span>Select all</span>
            </label>

            {groupData && (
              <Button
                onClick={() => handleAddModelClick(parseInt(groupData.id))}
                variant='ghost'
                size='sm'
                className='h-7 gap-1.5 px-2 text-xs sm:h-7 sm:px-2.5 sm:text-[0.8rem]'
              >
                <Plus className='h-4 w-4' />
                Add model
              </Button>
            )}

            {showDeleteAllButton && (
              <Button
                onClick={handleDeleteAll}
                variant='ghost'
                size='sm'
                className='text-muted-foreground hover:text-foreground h-7 px-2 text-xs sm:h-7 sm:px-2.5 sm:text-[0.8rem]'
                aria-label='Delete all overrides'
                disabled={
                  allOverrideModels.length === 0 ||
                  deleteAllOverridesMutation.isPending
                }
              >
                Delete all overrides
              </Button>
            )}
          </div>

          {selectedModels.size > 0 && (
            <div className='-mx-1 flex items-center gap-1.5 overflow-x-auto px-1 pb-1 sm:mx-0 sm:flex-wrap sm:overflow-visible sm:px-0 sm:pb-0'>
              <span className='text-muted-foreground shrink-0 text-xs sm:text-sm'>
                {selectedModels.size} selected
              </span>

              {selectedSoftDeletedModelIds.length > 0 && (
                <Button
                  onClick={() =>
                    bulkRestoreMutation.mutate(selectedSoftDeletedModelIds)
                  }
                  variant='outline'
                  size='sm'
                  className='shrink-0 gap-1.5'
                  disabled={bulkRestoreMutation.isPending}
                >
                  <CheckCircle className='h-4 w-4' />
                  Enable ({selectedSoftDeletedModelIds.length})
                </Button>
              )}

              <Button
                onClick={handleBulkSoftDelete}
                variant='outline'
                size='sm'
                className='shrink-0 gap-1.5'
                disabled={bulkSoftDeleteMutation.isPending}
              >
                <Ban className='h-4 w-4' />
                Disable
              </Button>

              <Button
                onClick={handleBulkDelete}
                variant='outline'
                size='sm'
                className='border-destructive/30 text-destructive hover:bg-destructive/10 hover:text-destructive shrink-0 gap-1.5'
                disabled={bulkDeleteMutation.isPending}
              >
                <Trash2 className='h-4 w-4' />
                Delete
              </Button>
            </div>
          )}
        </div>
      </div>

      {Object.keys(groupedModels).length === 0 ? (
        <div className='border-border/40 rounded-lg border border-dashed p-4 text-center sm:p-5'>
          <p className='text-muted-foreground text-sm'>
            Try broadening your search or switch to a different provider scope.
          </p>
        </div>
      ) : null}

      {/* Provider Groups or Filtered Models */}
      {Object.entries(groupedModels).map(([provider, providerModels]) => {
        if (filterProvider && provider !== filterProvider) return null;

        const groupData = groupDataMap.get(provider);

        return (
          <ModelProviderSection
            key={provider}
            provider={provider}
            providerModels={providerModels}
            displayUnit={displayUnit}
            usdPerSat={usdPerSat}
            filterProvider={filterProvider}
            groupData={groupData}
            selectedModels={selectedModels}
            onSelectProviderModels={() => selectProviderModels(provider)}
            onDeselectProviderModels={() => {
              const providerModelIds = providerModels.map((model) => model.id);
              const newSelected = new Set(selectedModels);
              providerModelIds.forEach((id) => newSelected.delete(id));
              setSelectedModels(newSelected);
            }}
            onEditGroup={() => {
              if (groupData) {
                setEditingGroup({
                  provider,
                  models: providerModels,
                  groupData,
                });
              }
            }}
            onRefreshProviderModels={() => handleProviderRefresh(provider)}
            onDeleteAllProviderModels={() => handleDeleteByProvider(provider)}
            onModelHover={setHoveredModelId}
            onModelToggleSelection={toggleModelSelection}
            onEditModel={handleEditModelClick}
            onOverrideModel={handleOverrideModelClick}
            onEnableModel={(modelId) => restoreMutation.mutate(modelId)}
            onDisableModel={handleSoftDeleteModel}
            onDeleteModel={handleDeleteModel}
            hasEffectiveApiKey={(model) => Boolean(getEffectiveApiKey(model))}
            hasIndividualSettings={hasIndividualSettings}
          />
        );
      })}

      {/* Forms and Dialogs */}
      {modelDialogState.providerId && (
        <AddProviderModelDialog
          providerId={modelDialogState.providerId}
          isOpen={modelDialogState.isOpen}
          onClose={() =>
            setModelDialogState((prev) => ({ ...prev, isOpen: false }))
          }
          onSuccess={handleModelUpdate}
          initialData={modelDialogState.initialData}
          mode={modelDialogState.mode}
        />
      )}

      {editingGroup && (
        <EditGroupForm
          provider={editingGroup.provider}
          models={editingGroup.models}
          groupSettings={{
            group_api_key: editingGroup.groupData.group_api_key,
            group_url: editingGroup.groupData.group_url,
          }}
          onGroupUpdate={handleGroupUpdate}
          onCancel={() => setEditingGroup(null)}
          isOpen={!!editingGroup}
        />
      )}

      {/* Confirmation Dialogs */}
      <AlertDialog
        open={bulkDeleteDialogOpen}
        onOpenChange={setBulkDeleteDialogOpen}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              Permanently Delete Selected Models
            </AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to permanently delete {selectedModels.size}{' '}
              selected models? This will completely remove them from the
              database and cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={confirmBulkDelete}
              className='bg-destructive text-destructive-foreground hover:bg-destructive/90'
            >
              Delete Models
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={deleteAllDialogOpen}
        onOpenChange={setDeleteAllDialogOpen}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              Permanently Delete All Model Overrides
            </AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to permanently delete ALL model overrides?
              This will remove {allOverrideModels.length} override entries from
              the database while keeping provider catalog models. This action
              cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={confirmDeleteAll}
              className='bg-destructive text-destructive-foreground hover:bg-destructive/90'
            >
              Delete All Overrides
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
