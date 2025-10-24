'use client';

import React, { useState, useMemo } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  type Model,
  type ManualModel,
  type GroupSettings,
} from '@/lib/api/schemas/models';
import { AdminService, type AdminModelGroup } from '@/lib/api/services/admin';
type ModelGroup = AdminModelGroup;
import { AddModelForm } from '@/components/AddModelForm';
import { EditModelForm } from '@/components/EditModelForm';
import { EditGroupForm } from '@/components/EditGroupForm';
import { CollectModelsDialog } from '@/components/CollectModelsDialog';
import { formatCost } from '@/lib/services/costValidation';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { Checkbox } from '@/components/ui/checkbox';
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  DropdownMenuSeparator,
} from '@/components/ui/dropdown-menu';
import {
  Cpu,
  Zap,
  Edit3,
  Users,
  MoreVertical,
  Key,
  Globe,
  RefreshCw,
  Trash2,
  AlertTriangle,
  CheckSquare,
  Square,
  Ban,
  CheckCircle,
} from 'lucide-react';
import { toast } from 'sonner';
import { cn } from '@/lib/utils';

interface ModelSelectorProps {
  filterProvider?: string;
  groupData?: ModelGroup;
  showProviderActions?: boolean;
}

export function ModelSelector({
  filterProvider,
  groupData,
  showProviderActions = false,
}: ModelSelectorProps) {
  const [selectedModelId, setSelectedModelId] = useState<string>('');
  const [, setHoveredModelId] = useState<string | null>(null);
  const [isAddFormOpen, setIsAddFormOpen] = useState(false);
  const [editingModel, setEditingModel] = useState<Model | null>(null);
  const [editingGroup, setEditingGroup] = useState<{
    provider: string;
    models: Model[];
    groupData: ModelGroup;
  } | null>(null);
  const [isCollectDialogOpen, setIsCollectDialogOpen] = useState(false);

  // Bulk selection state
  const [selectedModels, setSelectedModels] = useState<Set<string>>(new Set());
  const [bulkDeleteDialogOpen, setBulkDeleteDialogOpen] = useState(false);
  const [deleteAllDialogOpen, setDeleteAllDialogOpen] = useState(false);
  const [
    bulkApplyGroupSettingsDialogOpen,
    setBulkApplyGroupSettingsDialogOpen,
  ] = useState(false);

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

  // Filter models by provider if specified
  const filteredModels = useMemo(() => {
    if (!filterProvider) return models;
    return models.filter((model) => model.provider === filterProvider);
  }, [models, filterProvider]);

  // Bulk deletion mutations
  const bulkDeleteMutation = useMutation({
    mutationFn: async (modelIds: string[]) => {
      const selectedModelsList = models.filter((m) => modelIds.includes(m.id));
      const modelsByProvider = selectedModelsList.reduce<
        Record<string, Model[]>
      >((acc, model) => {
        const providerId = model.provider_id || 'unknown';
        if (!acc[providerId]) {
          acc[providerId] = [];
        }
        acc[providerId].push(model);
        return acc;
      }, {});

      let totalDeleted = 0;
      for (const [providerId, providerModels] of Object.entries(
        modelsByProvider
      )) {
        if (providerId === 'unknown') {
          continue;
        }
        const modelFullNames = providerModels.map((m) => m.full_name);
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
      queryClient.invalidateQueries({ queryKey: ['models-with-providers'] });
      queryClient.invalidateQueries({ queryKey: ['all-provider-models'] });
      queryClient.invalidateQueries({ queryKey: ['upstream-providers'] });
    },
    onError: (error) => {
      toast.error(`Failed to delete model overrides: ${error.message}`);
    },
  });

  // Bulk soft deletion mutations (disable models)
  const bulkSoftDeleteMutation = useMutation({
    mutationFn: async (modelIds: string[]) => {
      const selectedModelsList = models.filter((m) => modelIds.includes(m.id));
      const modelsByProvider = selectedModelsList.reduce<
        Record<string, Model[]>
      >((acc, model) => {
        const providerId = model.provider_id || 'unknown';
        if (!acc[providerId]) {
          acc[providerId] = [];
        }
        acc[providerId].push(model);
        return acc;
      }, {});

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
              model.full_name
            );
            await AdminService.updateProviderModel(
              providerIdNum,
              model.full_name,
              {
                ...existingModel,
                enabled: false,
              }
            );
            totalDisabled++;
          } catch (fetchError: unknown) {
            const error = fetchError as { message?: string; status?: number };
            if (error.message?.includes('404') || error.status === 404) {
              const newOverride = {
                id: model.full_name,
                name: model.name,
                description: model.description || '',
                created: Math.floor(Date.now() / 1000),
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
                  request: 0,
                  image: 0,
                  web_search: 0,
                  internal_reasoning: 0,
                },
                per_request_limits: null,
                top_provider: null,
                upstream_provider_id: providerIdNum,
                enabled: false,
              };
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
      queryClient.invalidateQueries({ queryKey: ['models-with-providers'] });
      queryClient.invalidateQueries({ queryKey: ['all-provider-models'] });
      queryClient.invalidateQueries({ queryKey: ['upstream-providers'] });
    },
    onError: (error) => {
      toast.error(`Failed to disable models: ${error.message}`);
    },
  });

  // Bulk apply group settings mutation
  const bulkApplyGroupSettingsMutation = useMutation({
    mutationFn: async (modelIds: string[]) => {
      if (!groupData) throw new Error('No group data available');

      const updates: { api_key?: string; url?: string } = {};

      // Set api_key to empty string to remove individual API keys (will use group key)
      updates.api_key = '';

      // Use group URL if available, otherwise use default
      if (groupData.group_url) {
        updates.url = groupData.group_url;
      }

      return AdminService.bulkUpdateModels(modelIds, updates);
    },
    onSuccess: (data) => {
      toast.success(
        `Successfully applied group settings to ${data.updated_count} models`
      );
      if (data.errors.length > 0) {
        toast.warning(`${data.errors.length} models had errors during update`);
        console.warn('Bulk update errors:', data.errors);
      }
      setSelectedModels(new Set());
      queryClient.invalidateQueries({ queryKey: ['models-with-providers'] });
      queryClient.invalidateQueries({ queryKey: ['all-provider-models'] });
      queryClient.invalidateQueries({ queryKey: ['upstream-providers'] });
    },
    onError: (error) => {
      toast.error(`Failed to apply group settings: ${error.message}`);
    },
  });

  const deleteAllMutation = useMutation({
    mutationFn: () => AdminService.deleteAllModels(),
    onSuccess: (data) => {
      toast.success(
        `Successfully permanently deleted ${data.deleted_count} models`
      );
      setSelectedModels(new Set());
      queryClient.invalidateQueries({ queryKey: ['models-with-providers'] });
      queryClient.invalidateQueries({ queryKey: ['all-provider-models'] });
      queryClient.invalidateQueries({ queryKey: ['upstream-providers'] });
    },
    onError: (error) => {
      toast.error(`Failed to permanently delete all models: ${error.message}`);
    },
  });

  const deleteByProviderMutation = useMutation({
    mutationFn: (providerId: string) =>
      AdminService.deleteModelsByProvider(providerId),
    onSuccess: (data) => {
      toast.success(
        `Successfully permanently deleted ${data.deleted_count} models from provider`
      );
      setSelectedModels(new Set());
      queryClient.invalidateQueries({ queryKey: ['models-with-providers'] });
      queryClient.invalidateQueries({ queryKey: ['all-provider-models'] });
      queryClient.invalidateQueries({ queryKey: ['upstream-providers'] });
    },
    onError: (error) => {
      toast.error(
        `Failed to permanently delete provider models: ${error.message}`
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
        model.full_name
      );

      await AdminService.updateProviderModel(providerId, model.full_name, {
        ...existingModel,
        enabled: true,
      });

      return { restored_count: 1 };
    },
    onSuccess: () => {
      toast.success('Model enabled successfully');
      queryClient.invalidateQueries({ queryKey: ['models-with-providers'] });
      queryClient.invalidateQueries({ queryKey: ['all-provider-models'] });
      queryClient.invalidateQueries({ queryKey: ['upstream-providers'] });
    },
    onError: (error) => {
      toast.error(`Failed to enable model: ${error.message}`);
    },
  });

  // Bulk restore mutation (enable models)
  const bulkRestoreMutation = useMutation({
    mutationFn: async (modelIds: string[]) => {
      const selectedModelsList = models.filter((m) => modelIds.includes(m.id));
      const modelsByProvider = selectedModelsList.reduce<
        Record<string, Model[]>
      >((acc, model) => {
        const providerId = model.provider_id || 'unknown';
        if (!acc[providerId]) {
          acc[providerId] = [];
        }
        acc[providerId].push(model);
        return acc;
      }, {});

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
              model.full_name
            );
            await AdminService.updateProviderModel(
              providerIdNum,
              model.full_name,
              {
                ...existingModel,
                enabled: true,
              }
            );
            totalEnabled++;
          } catch (error) {
            console.error(`Failed to enable model ${model.full_name}:`, error);
          }
        }
      }

      return { restored_count: totalEnabled, message: 'Models enabled' };
    },
    onSuccess: (data) => {
      toast.success(`Successfully enabled ${data.restored_count} models`);
      setSelectedModels(new Set());
      queryClient.invalidateQueries({ queryKey: ['models-with-providers'] });
      queryClient.invalidateQueries({ queryKey: ['all-provider-models'] });
      queryClient.invalidateQueries({ queryKey: ['upstream-providers'] });
    },
    onError: (error) => {
      toast.error(`Failed to enable models: ${error.message}`);
    },
  });

  // Group models by provider for better organization (only if not filtering)
  const groupedModels = useMemo(() => {
    if (filterProvider) {
      // If filtering by provider, return single group
      return { [filterProvider]: filteredModels };
    }

    if (!models) return {};

    return models.reduce<Record<string, Model[]>>((acc, model) => {
      const provider = model.provider;
      if (!acc[provider]) {
        acc[provider] = [];
      }
      acc[provider].push(model);
      return acc;
    }, {});
  }, [models, filteredModels, filterProvider]);

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

  // Handle manual model addition
  const handleAddModel = async (newModel: ManualModel) => {
    try {
      // Find or create the provider group
      let providerGroup = groups.find((g) => g.provider === newModel.provider);

      if (!providerGroup) {
        providerGroup = await AdminService.createModelGroup({
          provider: newModel.provider,
          group_api_key: undefined,
        });
      }

      const modelData = {
        name: newModel.name,
        full_name: newModel.name,
        input_cost: newModel.input_cost,
        output_cost: newModel.output_cost,
        provider: newModel.provider,
        modelType: newModel.modelType,
        description: newModel.description || undefined,
        contextLength: newModel.contextLength,
      };

      await AdminService.createModel(modelData);
      await refetchModels();
      toast.success(`Model "${newModel.name}" added successfully!`);
    } catch (error) {
      console.error('Error adding model:', error);
      toast.error('Failed to add model. Please try again.');
      throw error; // Re-throw to let the form handle the error
    }
  };

  // Handle model update
  const handleModelUpdate = async () => {
    await refetchModels();
    setEditingModel(null);
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
    } catch (error) {
      console.error('Error updating group:', error);
      toast.error('Failed to update group. Please try again.');
    }
  };

  // Handle model collection success
  const handleCollectSuccess = async () => {
    await refetchModels();
    setIsCollectDialogOpen(false);
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
      queryClient.invalidateQueries({ queryKey: ['models-with-providers'] });
      queryClient.invalidateQueries({ queryKey: ['all-provider-models'] });
      queryClient.invalidateQueries({ queryKey: ['upstream-providers'] });
    } catch (error: unknown) {
      console.error('Error refreshing provider models:', error);
      const errorMessage =
        error instanceof Error ? error.message : 'Unknown error';
      toast.error(`Failed to refresh ${provider} models: ${errorMessage}`);
    }
  };

  const getModelTypeIcon = (modelType: string) => {
    switch (modelType.toLowerCase()) {
      case 'text':
        return <Cpu className='h-4 w-4' />;
      case 'embedding':
        return <Zap className='h-4 w-4' />;
      case 'image':
        return <Globe className='h-4 w-4' />;
      default:
        return <Cpu className='h-4 w-4' />;
    }
  };

  const getModelTypeColor = (modelType: string) => {
    switch (modelType.toLowerCase()) {
      case 'text':
        return 'bg-blue-100 text-blue-800';
      case 'embedding':
        return 'bg-purple-100 text-purple-800';
      case 'image':
        return 'bg-green-100 text-green-800';
      case 'audio':
        return 'bg-orange-100 text-orange-800';
      case 'multimodal':
        return 'bg-pink-100 text-pink-800';
      default:
        return 'bg-gray-100 text-gray-800';
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
    setSelectedModels(new Set(filteredModels.map((m) => m.id)));
  };

  const deselectAllModels = () => {
    setSelectedModels(new Set());
  };

  const selectProviderModels = (provider: string) => {
    const providerModelIds = filteredModels
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

  const confirmBulkApplyGroupSettings = () => {
    bulkApplyGroupSettingsMutation.mutate(Array.from(selectedModels));
    setBulkApplyGroupSettingsDialogOpen(false);
  };

  const handleDeleteAll = () => {
    setDeleteAllDialogOpen(true);
  };

  const confirmDeleteAll = () => {
    deleteAllMutation.mutate();
    setDeleteAllDialogOpen(false);
  };

  const handleDeleteByProvider = (provider: string) => {
    const group = groupDataMap.get(provider);
    if (group?.id) {
      deleteByProviderMutation.mutate(group.id);
    }
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
      await AdminService.deleteProviderModel(providerId, model.full_name);
      toast.success('Model override deleted successfully');
      queryClient.invalidateQueries({ queryKey: ['models-with-providers'] });
      queryClient.invalidateQueries({ queryKey: ['all-provider-models'] });
      queryClient.invalidateQueries({ queryKey: ['upstream-providers'] });
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
          model.full_name
        );

        await AdminService.updateProviderModel(providerId, model.full_name, {
          ...existingModel,
          enabled: false,
        });
        toast.success('Model disabled successfully');
      } catch (fetchError: unknown) {
        const error = fetchError as { message?: string; status?: number };
        if (error.message?.includes('404') || error.status === 404) {
          const newOverride = {
            id: model.full_name,
            name: model.name,
            description: model.description || '',
            created: Math.floor(Date.now() / 1000),
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
              request: 0,
              image: 0,
              web_search: 0,
              internal_reasoning: 0,
            },
            per_request_limits: null,
            top_provider: null,
            upstream_provider_id: providerId,
            enabled: false,
          };

          await AdminService.createProviderModel(providerId, newOverride);
          toast.success('Model disabled successfully');
        } else {
          throw fetchError;
        }
      }

      queryClient.invalidateQueries({ queryKey: ['models-with-providers'] });
      queryClient.invalidateQueries({ queryKey: ['all-provider-models'] });
      queryClient.invalidateQueries({ queryKey: ['upstream-providers'] });
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
      <div className='rounded-lg border border-red-200 bg-red-50 p-4'>
        <p className='text-sm text-red-800'>
          Error loading models. Please try again later.
        </p>
      </div>
    );
  }

  return (
    <div className='grid gap-6'>
      {/* Action buttons */}
      <div className='flex flex-wrap items-center gap-2'>
        {/* Model Management Actions
        <Button onClick={() => setIsAddFormOpen(true)} className='gap-2'>
          <Plus className='h-4 w-4' />
          Add Model
        </Button>
        <Button
          onClick={() => setIsCollectDialogOpen(true)}
          variant='outline'
          className='gap-2'
        >
          Collect Models
        </Button>
        */}

        {showProviderActions && (
          <>
            <div className='bg-border h-6 w-px' />

            <Button
              onClick={() => {
                const allModelIds = filteredModels.map((m) => m.id);
                const newSelected = new Set(selectedModels);
                allModelIds.forEach((id) => newSelected.add(id));
                setSelectedModels(newSelected);
              }}
              variant='outline'
              size='sm'
              className='gap-2'
            >
              <CheckSquare className='h-4 w-4' />
              Select All ({filteredModels.length})
            </Button>

            {selectedModels.size > 0 && (
              <Button
                onClick={() => {
                  if (filterProvider) {
                    // If in provider view, deselect only models from this provider
                    const groupModelIds = filteredModels.map((m) => m.id);
                    const newSelected = new Set(selectedModels);
                    groupModelIds.forEach((id) => newSelected.delete(id));
                    setSelectedModels(newSelected);
                  } else {
                    // If in all view, deselect all
                    setSelectedModels(new Set());
                  }
                }}
                variant='outline'
                size='sm'
                className='gap-2'
              >
                <Square className='h-4 w-4' />
                Deselect All
              </Button>
            )}
          </>
        )}

        {/* Bulk selection actions */}
        {selectedModels.size > 0 && (
          <>
            <div className='bg-border h-6 w-px' />
            <span className='text-muted-foreground text-sm'>
              {selectedModels.size} selected
            </span>

            {(() => {
              const selectedSoftDeletedModels = filteredModels.filter(
                (m) => selectedModels.has(m.id) && m.soft_deleted
              );
              return (
                selectedSoftDeletedModels.length > 0 && (
                  <Button
                    onClick={() =>
                      bulkRestoreMutation.mutate(
                        selectedSoftDeletedModels.map((m) => m.id)
                      )
                    }
                    variant='outline'
                    size='sm'
                    className='gap-2 border-green-300 text-green-600 hover:border-green-400 hover:text-green-700'
                    disabled={bulkRestoreMutation.isPending}
                  >
                    <CheckCircle className='h-4 w-4' />
                    Enable Selected ({selectedSoftDeletedModels.length})
                  </Button>
                )
              );
            })()}

            <Button
              onClick={handleBulkDelete}
              variant='destructive'
              size='sm'
              className='gap-2'
              disabled={bulkDeleteMutation.isPending}
            >
              <Trash2 className='h-4 w-4' />
              Delete Selected Overrides
            </Button>
            <Button
              onClick={handleBulkSoftDelete}
              variant='outline'
              size='sm'
              className='gap-2 border-orange-300 text-orange-600 hover:border-orange-400 hover:text-orange-700'
              disabled={bulkSoftDeleteMutation.isPending}
            >
              <Ban className='h-4 w-4' />
              Disable Selected
            </Button>
          </>
        )}

        {/* Global actions */}
        {!showProviderActions && (
          <>
            <div className='bg-border h-6 w-px' />
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant='outline' size='sm' className='gap-2'>
                  <MoreVertical className='h-4 w-4' />
                  More Actions
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align='end'>
                <DropdownMenuItem onClick={selectAllModels}>
                  <CheckSquare className='mr-2 h-4 w-4' />
                  Select All Models
                </DropdownMenuItem>
                <DropdownMenuItem onClick={deselectAllModels}>
                  <Square className='mr-2 h-4 w-4' />
                  Deselect All
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  onClick={handleDeleteAll}
                  className='text-destructive focus:text-destructive'
                >
                  <AlertTriangle className='mr-2 h-4 w-4' />
                  Delete All Models Permanently
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </>
        )}
      </div>

      {/* Provider Groups or Filtered Models */}
      {Object.entries(groupedModels).map(([provider, providerModels]) => {
        if (filterProvider && provider !== filterProvider) return null;

        const groupData = groupDataMap.get(provider);
        const allProviderSelected = providerModels.every((m) =>
          selectedModels.has(m.id)
        );
        const someProviderSelected = providerModels.some((m) =>
          selectedModels.has(m.id)
        );

        return (
          <Card key={provider} className='overflow-hidden'>
            {!filterProvider && (
              <CardHeader className='pb-3'>
                <div className='flex items-center justify-between'>
                  <div className='flex items-center gap-3'>
                    <Checkbox
                      checked={allProviderSelected}
                      ref={(input) => {
                        if (input && 'indeterminate' in input) {
                          (input as HTMLInputElement).indeterminate =
                            someProviderSelected && !allProviderSelected;
                        }
                      }}
                      onCheckedChange={(checked) => {
                        if (checked) {
                          selectProviderModels(provider);
                        } else {
                          const providerModelIds = providerModels.map(
                            (m) => m.id
                          );
                          const newSelected = new Set(selectedModels);
                          providerModelIds.forEach((id) =>
                            newSelected.delete(id)
                          );
                          setSelectedModels(newSelected);
                        }
                      }}
                    />
                    <div>
                      <CardTitle className='flex items-center gap-2 text-lg'>
                        <Users className='h-5 w-5' />
                        {provider}
                        <span className='text-muted-foreground text-sm font-normal'>
                          ({providerModels.length} models)
                        </span>
                      </CardTitle>
                      <CardDescription>
                        {groupData?.group_url ? (
                          <span className='flex items-center gap-1'>
                            <Globe className='h-3 w-3' />
                            {groupData.group_url}
                          </span>
                        ) : (
                          'Using default endpoint'
                        )}
                        {groupData?.group_api_key && (
                          <span className='ml-2 flex items-center gap-1'>
                            <Key className='h-3 w-3' />
                            Group API Key
                          </span>
                        )}
                      </CardDescription>
                    </div>
                  </div>
                  <div className='flex items-center gap-2'>
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button variant='ghost' size='sm'>
                          <MoreVertical className='h-4 w-4' />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align='end'>
                        <DropdownMenuItem
                          onClick={() => {
                            if (groupData) {
                              setEditingGroup({
                                provider,
                                models: providerModels,
                                groupData,
                              });
                            }
                          }}
                        >
                          <Edit3 className='mr-2 h-4 w-4' />
                          Edit Group
                        </DropdownMenuItem>
                        <DropdownMenuItem
                          onClick={() => selectProviderModels(provider)}
                        >
                          <CheckSquare className='mr-2 h-4 w-4' />
                          Select All
                        </DropdownMenuItem>
                        <DropdownMenuItem
                          onClick={() => handleProviderRefresh(provider)}
                        >
                          <RefreshCw className='mr-2 h-4 w-4' />
                          Refresh Models
                        </DropdownMenuItem>
                        <DropdownMenuSeparator />
                        <DropdownMenuItem
                          onClick={() => handleDeleteByProvider(provider)}
                          className='text-destructive focus:text-destructive'
                        >
                          <Trash2 className='mr-2 h-4 w-4' />
                          Delete All Models Permanently
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </div>
                </div>
              </CardHeader>
            )}
            <CardContent className={filterProvider ? 'pt-6' : 'pt-0'}>
              <div className='grid gap-3 md:grid-cols-2 lg:grid-cols-3'>
                {providerModels.map((model) => (
                  <Card
                    key={model.id}
                    className={cn(
                      'relative cursor-pointer overflow-hidden transition-all duration-200 hover:shadow-md',
                      selectedModels.has(model.id) && 'ring-primary ring-2',
                      selectedModelId === model.id && 'ring-2 ring-blue-500',
                      model.soft_deleted &&
                        'border-red-200 bg-red-50 opacity-75'
                    )}
                    onMouseEnter={() => setHoveredModelId(model.id)}
                    onMouseLeave={() => setHoveredModelId(null)}
                  >
                    <CardHeader className='pb-2'>
                      <div className='flex items-start gap-2 pr-10'>
                        <Checkbox
                          checked={selectedModels.has(model.id)}
                          onCheckedChange={() => toggleModelSelection(model.id)}
                          onClick={(e) => e.stopPropagation()}
                          className='mt-1 flex-shrink-0'
                        />
                        <div className='flex w-[calc(100%-3rem)] items-start gap-2 overflow-hidden'>
                          <div className='mt-1 flex-shrink-0'>
                            {getModelTypeIcon(model.modelType)}
                          </div>
                          <div className='w-full overflow-hidden'>
                            <h3
                              className={cn(
                                'overflow-hidden text-sm font-medium text-ellipsis whitespace-nowrap',
                                model.soft_deleted &&
                                  'text-red-600 line-through'
                              )}
                            >
                              {model.name}
                            </h3>
                            {model.full_name !== model.name && (
                              <p className='text-muted-foreground overflow-hidden text-xs text-ellipsis whitespace-nowrap'>
                                {model.full_name}
                              </p>
                            )}
                          </div>
                        </div>
                        <div className='absolute top-3 right-2'>
                          <DropdownMenu>
                            <DropdownMenuTrigger asChild>
                              <Button
                                variant='ghost'
                                size='sm'
                                onClick={(e) => e.stopPropagation()}
                                className='h-8 w-8 p-0'
                              >
                                <MoreVertical className='h-4 w-4' />
                              </Button>
                            </DropdownMenuTrigger>
                            <DropdownMenuContent align='end'>
                              <DropdownMenuItem
                                onClick={(e) => {
                                  e.stopPropagation();
                                  setEditingModel(model);
                                }}
                              >
                                <Edit3 className='mr-2 h-4 w-4' />
                                Edit Model
                              </DropdownMenuItem>
                              <DropdownMenuSeparator />
                              {model.soft_deleted ? (
                                <DropdownMenuItem
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    restoreMutation.mutate(model.id);
                                  }}
                                  className='text-green-600 focus:text-green-700'
                                >
                                  <CheckCircle className='mr-2 h-4 w-4' />
                                  Enable Model
                                </DropdownMenuItem>
                              ) : (
                                <DropdownMenuItem
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    handleSoftDeleteModel(model.id);
                                  }}
                                  className='text-orange-600 focus:text-orange-700'
                                >
                                  <Ban className='mr-2 h-4 w-4' />
                                  Disable Model
                                </DropdownMenuItem>
                              )}
                              <DropdownMenuItem
                                onClick={(e) => {
                                  e.stopPropagation();
                                  handleDeleteModel(model.id);
                                }}
                                className='text-destructive focus:text-destructive'
                              >
                                <Trash2 className='mr-2 h-4 w-4' />
                                Delete
                              </DropdownMenuItem>
                            </DropdownMenuContent>
                          </DropdownMenu>
                        </div>
                      </div>
                    </CardHeader>
                    <CardContent
                      className='space-y-2 pt-0'
                      onClick={() => setSelectedModelId(model.id)}
                    >
                      <div className='flex flex-wrap gap-1'>
                        <span
                          className={cn(
                            'inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium',
                            getModelTypeColor(model.modelType)
                          )}
                        >
                          {model.modelType}
                        </span>
                        {model.api_key_type === 'remote' && (
                          <span className='inline-flex items-center rounded-full border border-blue-200 bg-blue-100 px-2.5 py-0.5 text-xs font-medium text-blue-800'>
                            Remote
                          </span>
                        )}
                        {model.api_key_type !== 'remote' && (
                          <span className='inline-flex items-center rounded-full border border-emerald-200 bg-emerald-100 px-2.5 py-0.5 text-xs font-medium text-emerald-800'>
                            Database
                          </span>
                        )}
                        {model.is_free && (
                          <span className='inline-flex items-center rounded-full bg-yellow-100 px-2.5 py-0.5 text-xs font-medium text-yellow-800'>
                            Free
                          </span>
                        )}
                        {!getEffectiveApiKey(model) && (
                          <span className='inline-flex items-center rounded-full bg-red-100 px-2.5 py-0.5 text-xs font-medium text-red-800'>
                            <Key className='mr-1 h-3 w-3' />
                            No API Key
                          </span>
                        )}
                        {getEffectiveApiKey(model) &&
                          !hasIndividualSettings(model) && (
                            <span className='inline-flex items-center rounded-full bg-green-100 px-2.5 py-0.5 text-xs font-medium text-green-800'>
                              <Users className='mr-1 h-3 w-3' />
                              Group Key
                            </span>
                          )}
                        {hasIndividualSettings(model) && (
                          <span className='inline-flex items-center rounded-full bg-purple-100 px-2.5 py-0.5 text-xs font-medium text-purple-800'>
                            <Key className='mr-1 h-3 w-3' />
                            Individual Key
                          </span>
                        )}
                        {model.soft_deleted && (
                          <span className='inline-flex items-center rounded-full border border-red-300 bg-red-100 px-2.5 py-0.5 text-xs font-medium text-red-800'>
                            <Trash2 className='mr-1 h-3 w-3' />
                            Deleted
                          </span>
                        )}
                      </div>

                      {!model.is_free && (
                        <div className='text-muted-foreground space-y-1 text-xs'>
                          <div className='flex justify-between'>
                            <span className='truncate'>Input:</span>
                            <span className='ml-2 truncate'>
                              {formatCost(model.input_cost)}/1M tokens
                            </span>
                          </div>
                          <div className='flex justify-between'>
                            <span className='truncate'>Output:</span>
                            <span className='ml-2 truncate'>
                              {formatCost(model.output_cost)}/1M tokens
                            </span>
                          </div>
                        </div>
                      )}

                      {model.description && (
                        <p className='text-muted-foreground line-clamp-2 overflow-hidden text-xs break-words text-ellipsis'>
                          {model.description}
                        </p>
                      )}
                    </CardContent>
                  </Card>
                ))}
              </div>
            </CardContent>
          </Card>
        );
      })}

      {/* Forms and Dialogs */}
      <AddModelForm
        isOpen={isAddFormOpen}
        onModelAdd={handleAddModel}
        onCancel={() => setIsAddFormOpen(false)}
      />

      {editingModel && (
        <EditModelForm
          model={editingModel}
          providerId={
            editingModel.provider_id
              ? parseInt(editingModel.provider_id)
              : undefined
          }
          onModelUpdate={handleModelUpdate}
          onCancel={() => setEditingModel(null)}
          isOpen={!!editingModel}
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

      <CollectModelsDialog
        isOpen={isCollectDialogOpen}
        onClose={() => setIsCollectDialogOpen(false)}
        onSuccess={handleCollectSuccess}
      />

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
        open={bulkApplyGroupSettingsDialogOpen}
        onOpenChange={setBulkApplyGroupSettingsDialogOpen}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              Apply Group Settings to{' '}
              {selectedModels.size === filteredModels.length && filterProvider
                ? `All Models in ${filterProvider}`
                : 'Selected Models'}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {selectedModels.size === filteredModels.length &&
              filterProvider ? (
                <div>
                  <p>
                    Are you sure you want to force ALL {selectedModels.size}{' '}
                    models in the <strong>{filterProvider}</strong> group to use
                    group settings?
                  </p>
                  <p className='mt-2 text-sm text-amber-600'>
                    <strong>
                      ⚠️ This will override individual model configurations:
                    </strong>
                  </p>
                </div>
              ) : (
                <p>
                  Are you sure you want to apply the group settings to{' '}
                  {selectedModels.size} selected models?
                </p>
              )}
              <ul className='mt-2 list-inside list-disc space-y-1 text-sm'>
                <li>
                  Remove individual API keys (models will use the group API key)
                </li>
                {groupData?.group_url && (
                  <li>
                    Set the URL to:{' '}
                    <code className='rounded bg-gray-100 px-1'>
                      {groupData.group_url}
                    </code>
                  </li>
                )}
                <li>Force models to use group configurations</li>
                {selectedModels.size === filteredModels.length &&
                  filterProvider && (
                    <li className='font-medium text-amber-600'>
                      This will affect ALL models in this provider group
                    </li>
                  )}
              </ul>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={confirmBulkApplyGroupSettings}
              className='bg-blue-600 text-white hover:bg-blue-700'
            >
              {selectedModels.size === filteredModels.length && filterProvider
                ? `Force All ${selectedModels.size} Models`
                : 'Apply Group Settings'}
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
            <AlertDialogTitle>Permanently Delete All Models</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to permanently delete ALL models? This will
              completely remove all {models.length} models from the database.
              This action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={confirmDeleteAll}
              className='bg-destructive text-destructive-foreground hover:bg-destructive/90'
            >
              Delete All Models
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
