'use client';

import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  AdminService,
  ProviderModels,
  ProviderType,
  UpstreamProvider,
  CreateUpstreamProvider,
  UpdateUpstreamProvider,
  AdminModel,
} from '@/lib/api/services/admin';
import { AddProviderModelDialog } from '@/components/add-provider-model-dialog';
import { BatchOverrideDialog } from '@/components/batch-override-dialog';
import { ProviderFeeScheduleModal } from '@/components/provider-fee-schedule-modal';
import { ProviderCard } from '@/components/provider-card';
import { ProviderFormDialogContent } from '@/components/provider-form-dialog-content';
import { Skeleton } from '@/components/ui/skeleton';
import { AlertCircle, Clock, Plus, Server } from 'lucide-react';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Dialog, DialogTrigger } from '@/components/ui/dialog';
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from '@/components/ui/empty';
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
import { useMemo, useState } from 'react';
import { toast } from 'sonner';
import { AppPageShell } from '@/components/app-page-shell';
import { PageHeader } from '@/components/page-header';

const apiKeyDocsLinkClassName =
  'text-primary text-xs underline-offset-4 hover:underline';

export default function ProvidersPage() {
  const queryClient = useQueryClient();
  const [editingProvider, setEditingProvider] =
    useState<UpstreamProvider | null>(null);
  const [isCreateDialogOpen, setIsCreateDialogOpen] = useState(false);
  const [isEditDialogOpen, setIsEditDialogOpen] = useState(false);
  const [expandedProviders, setExpandedProviders] = useState<Set<number>>(
    new Set()
  );
  const [viewingModels, setViewingModels] = useState<number | null>(null);
  const [isCreatingAccount, setIsCreatingAccount] = useState(false);
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
  const [batchOverrideProviderId, setBatchOverrideProviderId] = useState<
    number | null
  >(null);
  const [feeScheduleState, setFeeScheduleState] = useState<{
    open: boolean;
    initialIds: number[];
  }>({ open: false, initialIds: [] });
  const [providerDeleteTarget, setProviderDeleteTarget] =
    useState<UpstreamProvider | null>(null);
  const [modelDeleteTarget, setModelDeleteTarget] = useState<{
    providerId: number;
    modelId: string;
  } | null>(null);

  const [formData, setFormData] = useState<CreateUpstreamProvider>({
    provider_type: 'openrouter',
    base_url: 'https://openrouter.ai/api/v1',
    api_key: '',
    api_version: null,
    enabled: true,
    provider_fee: 1.06,
    provider_settings: {},
  });

  const getProviderFeePlaceholder = (type: string) => {
    return type === 'openrouter' ? 'Default: 1.06 (6%)' : 'Default: 1.01 (1%)';
  };

  const { data: providerTypes = [] } = useQuery({
    queryKey: ['provider-types'],
    queryFn: () => AdminService.getProviderTypes(),
    refetchOnWindowFocus: false,
  });

  const providerTypeById = useMemo(
    () => new Map<string, ProviderType>(providerTypes.map((pt) => [pt.id, pt])),
    [providerTypes]
  );

  const { data: globalSettings } = useQuery({
    queryKey: ['settings'],
    queryFn: () => AdminService.getSettings(),
    refetchOnWindowFocus: false,
  });

  const {
    data: providers = [],
    isLoading,
    error,
  } = useQuery({
    queryKey: ['upstream-providers'],
    queryFn: () => AdminService.getUpstreamProviders(),
    refetchOnWindowFocus: false,
  });

  const { data: providerModels, isLoading: isLoadingModels } =
    useQuery<ProviderModels | null>({
      queryKey: ['provider-models', viewingModels],
      queryFn: () =>
        viewingModels
          ? AdminService.getProviderModels(viewingModels)
          : Promise.resolve(null),
      enabled: !!viewingModels,
      refetchOnWindowFocus: false,
    });

  const createMutation = useMutation({
    mutationFn: (data: CreateUpstreamProvider) =>
      AdminService.createUpstreamProvider(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['upstream-providers'] });
      setIsCreateDialogOpen(false);
      toast.success('Provider created successfully');
      resetForm();
    },
    onError: (error: Error) => {
      toast.error(`Failed to create provider: ${error.message}`);
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: number; data: UpdateUpstreamProvider }) =>
      AdminService.updateUpstreamProvider(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['upstream-providers'] });
      setIsEditDialogOpen(false);
      setEditingProvider(null);
      toast.success('Provider updated successfully');
    },
    onError: (error: Error) => {
      toast.error(`Failed to update provider: ${error.message}`);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => AdminService.deleteUpstreamProvider(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['upstream-providers'] });
      toast.success('Provider deleted successfully');
    },
    onError: (error: Error) => {
      toast.error(`Failed to delete provider: ${error.message}`);
    },
  });

  const deleteModelMutation = useMutation({
    mutationFn: ({
      providerId,
      modelId,
    }: {
      providerId: number;
      modelId: string;
    }) => AdminService.deleteProviderModel(providerId, modelId),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({
        queryKey: ['provider-models', variables.providerId],
      });
      toast.success('Model deleted successfully');
    },
    onError: (error: Error) => {
      toast.error(`Failed to delete model: ${error.message}`);
    },
  });

  const handleCreateAccount = async () => {
    setIsCreatingAccount(true);
    try {
      const response = await AdminService.createProviderAccountByType(
        formData.provider_type
      );
      if (response.ok && response.account_data.api_key) {
        setFormData({
          ...formData,
          api_key: String(response.account_data.api_key),
        });
        toast.success(
          'Account created successfully! API key has been filled in.'
        );
      } else {
        toast.success('Account created, but no API key returned.');
      }
    } catch (error: unknown) {
      const errorMessage =
        error instanceof Error ? error.message : 'Unknown error';
      toast.error(`Failed to create account: ${errorMessage}`);
    } finally {
      setIsCreatingAccount(false);
    }
  };

  const resetForm = () => {
    setFormData({
      provider_type: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1',
      api_key: '',
      api_version: null,
      enabled: true,
      provider_fee: 1.06,
      provider_settings: {},
    });
  };

  const handleCreate = () => {
    const data = { ...formData };
    if (data.provider_fee === undefined || data.provider_fee === null) {
      data.provider_fee = data.provider_type === 'openrouter' ? 1.06 : 1.01;
    }
    createMutation.mutate(data);
  };

  const handleEdit = (provider: UpstreamProvider) => {
    setEditingProvider(provider);
    setFormData({
      provider_type: provider.provider_type,
      base_url: provider.base_url,
      api_key: '',
      api_version: provider.api_version || null,
      enabled: provider.enabled,
      provider_fee: provider.provider_fee,
      provider_fee_default: provider.provider_fee_default,
      provider_settings: provider.provider_settings || {},
    });
    setIsEditDialogOpen(true);
  };

  const handleUpdate = () => {
    if (!editingProvider) return;
    const updateData: UpdateUpstreamProvider = {
      provider_type: formData.provider_type,
      base_url: formData.base_url,
      api_version: formData.api_version,
      enabled: formData.enabled,
      provider_fee_default: formData.provider_fee_default,
      provider_settings: formData.provider_settings,
    };
    if (formData.api_key) {
      updateData.api_key = formData.api_key;
    }
    updateMutation.mutate({ id: editingProvider.id, data: updateData });
  };

  const confirmDeleteProvider = () => {
    if (!providerDeleteTarget) {
      return;
    }

    deleteMutation.mutate(providerDeleteTarget.id);
    setProviderDeleteTarget(null);
  };

  const confirmDeleteModel = () => {
    if (!modelDeleteTarget) {
      return;
    }

    deleteModelMutation.mutate(modelDeleteTarget);
    setModelDeleteTarget(null);
  };

  const getPlatformUrl = (type: string) => {
    const providerType = providerTypeById.get(type);
    return providerType?.platform_url || null;
  };

  const canCreateAccount = (type: string) => {
    const providerType = providerTypeById.get(type);
    return providerType?.can_create_account || false;
  };

  const canShowBalance = (type: string) => {
    const providerType = providerTypeById.get(type);
    return providerType?.can_show_balance || false;
  };

  const toggleProviderExpansion = (providerId: number) => {
    const newExpanded = new Set(expandedProviders);
    if (newExpanded.has(providerId)) {
      newExpanded.delete(providerId);
    } else {
      newExpanded.add(providerId);
    }
    setExpandedProviders(newExpanded);
    if (!newExpanded.has(providerId)) {
      setViewingModels(null);
    } else {
      setViewingModels(providerId);
    }
  };

  const handleAddModel = (providerId: number) => {
    setModelDialogState({
      isOpen: true,
      providerId,
      mode: 'create',
      initialData: null,
    });
  };

  const handleEditModel = (providerId: number, model: AdminModel) => {
    setModelDialogState({
      isOpen: true,
      providerId,
      mode: 'edit',
      initialData: model,
    });
  };

  const handleOverrideModel = (providerId: number, model: AdminModel) => {
    setModelDialogState({
      isOpen: true,
      providerId,
      mode: 'override',
      initialData: model,
    });
  };

  const handleBatchOverride = (providerId: number) => {
    setBatchOverrideProviderId(providerId);
  };

  const handleManageFeeSchedules = (providerId?: number) => {
    setFeeScheduleState({
      open: true,
      initialIds: providerId !== undefined ? [providerId] : [],
    });
  };

  const availableMints = (globalSettings?.cashu_mints as string[]) || [];

  return (
    <AppPageShell contentClassName='mx-auto w-full max-w-5xl'>
      <div className='@container/main flex flex-col gap-4 md:gap-8'>
        <Dialog open={isCreateDialogOpen} onOpenChange={setIsCreateDialogOpen}>
          <PageHeader
            title='Upstream Providers'
            description='Manage your AI provider connections and credentials.'
            actions={
              <div className='flex gap-2'>
                <Button
                  variant='outline'
                  onClick={() => handleManageFeeSchedules()}
                  disabled={providers.length === 0}
                >
                  <Clock className='h-4 w-4' />
                  Fee Schedules
                </Button>
                <DialogTrigger asChild>
                  <Button>
                    <Plus className='h-4 w-4' />
                    Add Provider
                  </Button>
                </DialogTrigger>
              </div>
            }
          />
          <ProviderFormDialogContent
            mode='create'
            title='Add Upstream Provider'
            description='Configure a new AI provider connection.'
            submitLabel='Create'
            submittingLabel='Creating...'
            formData={formData}
            setFormData={setFormData}
            providerTypes={providerTypes}
            providerFeePlaceholder={getProviderFeePlaceholder(
              formData.provider_type
            )}
            docsLinkClassName={apiKeyDocsLinkClassName}
            canCreateAccount={canCreateAccount(formData.provider_type)}
            isCreatingAccount={isCreatingAccount}
            onCreateAccount={handleCreateAccount}
            onCancel={() => setIsCreateDialogOpen(false)}
            onSubmit={handleCreate}
            isSubmitting={createMutation.isPending}
            availableMints={availableMints}
          />
        </Dialog>

        {isLoading ? (
          <div className='space-y-4'>
            <Skeleton className='h-[100px] w-full' />
            <Skeleton className='h-[100px] w-full' />
          </div>
        ) : error ? (
          <Alert variant='destructive'>
            <AlertCircle className='h-4 w-4' />
            <AlertDescription>
              Failed to load providers. Please try refreshing the page.
            </AlertDescription>
          </Alert>
        ) : providers.length === 0 ? (
          <Card>
            <CardContent className='py-12'>
              <Empty>
                <EmptyHeader>
                  <EmptyMedia variant='icon'>
                    <Server className='h-4 w-4' />
                  </EmptyMedia>
                  <EmptyTitle>No providers configured</EmptyTitle>
                  <EmptyDescription>
                    Get started by adding your first upstream provider.
                  </EmptyDescription>
                </EmptyHeader>
                <Button onClick={() => setIsCreateDialogOpen(true)}>
                  <Plus className='mr-2 h-4 w-4' />
                  Add Provider
                </Button>
              </Empty>
            </CardContent>
          </Card>
        ) : (
          <div className='grid gap-3 sm:gap-4'>
            {providers.map((provider) => (
              <ProviderCard
                key={provider.id}
                provider={provider}
                isExpanded={expandedProviders.has(provider.id)}
                canShowBalance={canShowBalance(provider.provider_type)}
                platformUrl={getPlatformUrl(provider.provider_type)}
                isModelsLoading={
                  isLoadingModels && viewingModels === provider.id
                }
                providerModels={
                  viewingModels === provider.id
                    ? (providerModels ?? null)
                    : null
                }
                isDeletingModel={deleteModelMutation.isPending}
                onToggleExpansion={() => toggleProviderExpansion(provider.id)}
                onEditProvider={() => handleEdit(provider)}
                onDeleteProvider={() => setProviderDeleteTarget(provider)}
                onBatchOverride={() => handleBatchOverride(provider.id)}
                onManageFeeSchedules={() =>
                  handleManageFeeSchedules(provider.id)
                }
                onAddModel={() => handleAddModel(provider.id)}
                onEditModel={(model) => handleEditModel(provider.id, model)}
                onDeleteModel={(modelId) =>
                  setModelDeleteTarget({ providerId: provider.id, modelId })
                }
                onOverrideModel={(model) =>
                  handleOverrideModel(provider.id, model)
                }
                onUpdateApiKey={(newKey) => {
                  updateMutation.mutate({
                    id: provider.id,
                    data: { api_key: newKey },
                  });
                }}
                availableMints={availableMints}
              />
            ))}
          </div>
        )}
      </div>

      <Dialog open={isEditDialogOpen} onOpenChange={setIsEditDialogOpen}>
        <ProviderFormDialogContent
          mode='edit'
          title='Edit Upstream Provider'
          description='Update provider configuration.'
          submitLabel='Update'
          submittingLabel='Updating...'
          formData={formData}
          setFormData={setFormData}
          providerTypes={providerTypes}
          providerFeePlaceholder={getProviderFeePlaceholder(
            formData.provider_type
          )}
          docsLinkClassName={apiKeyDocsLinkClassName}
          canCreateAccount={false}
          isCreatingAccount={false}
          onCreateAccount={handleCreateAccount}
          onCancel={() => setIsEditDialogOpen(false)}
          onSubmit={handleUpdate}
          isSubmitting={updateMutation.isPending}
          availableMints={availableMints}
        />
      </Dialog>

      <AlertDialog
        open={Boolean(providerDeleteTarget)}
        onOpenChange={(open) => !open && setProviderDeleteTarget(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Provider?</AlertDialogTitle>
            <AlertDialogDescription>
              This will permanently delete provider{' '}
              <span className='font-medium'>
                {providerDeleteTarget?.provider_type}
              </span>{' '}
              and remove its associated configuration.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={confirmDeleteProvider}
              className='bg-destructive text-destructive-foreground hover:bg-destructive/90'
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={Boolean(modelDeleteTarget)}
        onOpenChange={(open) => !open && setModelDeleteTarget(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Model Override?</AlertDialogTitle>
            <AlertDialogDescription>
              This removes the override for model{' '}
              <span className='font-medium'>{modelDeleteTarget?.modelId}</span>.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={confirmDeleteModel}
              className='bg-destructive text-destructive-foreground hover:bg-destructive/90'
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {modelDialogState.providerId && (
        <AddProviderModelDialog
          providerId={modelDialogState.providerId}
          isOpen={modelDialogState.isOpen}
          onClose={() =>
            setModelDialogState((prev) => ({ ...prev, isOpen: false }))
          }
          onSuccess={() => {
            queryClient.invalidateQueries({
              queryKey: ['provider-models', modelDialogState.providerId],
            });
          }}
          initialData={modelDialogState.initialData}
          mode={modelDialogState.mode}
        />
      )}

      {batchOverrideProviderId && (
        <BatchOverrideDialog
          providerId={batchOverrideProviderId}
          isOpen={!!batchOverrideProviderId}
          onClose={() => setBatchOverrideProviderId(null)}
          onSuccess={() => {
            queryClient.invalidateQueries({
              queryKey: ['provider-models', batchOverrideProviderId],
            });
          }}
        />
      )}

      <ProviderFeeScheduleModal
        providers={providers}
        initialSelectedIds={feeScheduleState.initialIds}
        isOpen={feeScheduleState.open}
        onClose={() => setFeeScheduleState({ open: false, initialIds: [] })}
        onSuccess={() => {
          queryClient.invalidateQueries({ queryKey: ['upstream-providers'] });
        }}
      />
    </AppPageShell>
  );
}
