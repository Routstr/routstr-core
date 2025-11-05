'use client';

import { SidebarInset, SidebarProvider } from '@/components/ui/sidebar';
import { AppSidebar } from '@/components/app-sidebar';
import { SiteHeader } from '@/components/site-header';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  AdminService,
  UpstreamProvider,
  CreateUpstreamProvider,
  UpdateUpstreamProvider,
} from '@/lib/api/services/admin';
import { Skeleton } from '@/components/ui/skeleton';
import {
  AlertCircle,
  Plus,
  Pencil,
  Trash2,
  Server,
  Database,
  ChevronDown,
  ChevronUp,
} from 'lucide-react';
import { Alert, AlertDescription } from '@/components/ui/alert';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useState } from 'react';
import { toast } from 'sonner';

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

  const [formData, setFormData] = useState<CreateUpstreamProvider>({
    provider_type: 'openrouter',
    base_url: 'https://openrouter.ai/api/v1',
    api_key: '',
    api_version: null,
    enabled: true,
  });

  const { data: providerTypes = [] } = useQuery({
    queryKey: ['provider-types'],
    queryFn: () => AdminService.getProviderTypes(),
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

  const { data: providerModels, isLoading: isLoadingModels } = useQuery({
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

  const resetForm = () => {
    setFormData({
      provider_type: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1',
      api_key: '',
      api_version: null,
      enabled: true,
    });
  };

  const handleCreate = () => {
    createMutation.mutate(formData);
  };

  const handleEdit = (provider: UpstreamProvider) => {
    setEditingProvider(provider);
    setFormData({
      provider_type: provider.provider_type,
      base_url: provider.base_url,
      api_key: '',
      api_version: provider.api_version || null,
      enabled: provider.enabled,
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
    };
    if (formData.api_key) {
      updateData.api_key = formData.api_key;
    }
    updateMutation.mutate({ id: editingProvider.id, data: updateData });
  };

  const handleDelete = (id: number) => {
    if (confirm('Are you sure you want to delete this provider?')) {
      deleteMutation.mutate(id);
    }
  };

  const getDefaultBaseUrl = (type: string) => {
    const providerType = providerTypes.find((pt) => pt.id === type);
    return providerType?.default_base_url || '';
  };

  const hasFixedBaseUrl = (type: string) => {
    const providerType = providerTypes.find((pt) => pt.id === type);
    return providerType?.fixed_base_url || false;
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

  return (
    <SidebarProvider>
      <AppSidebar variant='inset' />
      <SidebarInset>
        <SiteHeader />
        <div className='flex flex-1 flex-col'>
          <div className='@container/main flex flex-1 flex-col gap-4 p-4 md:gap-8 md:p-8'>
            <div className='mb-6 flex items-center justify-between'>
              <div>
                <h1 className='text-2xl font-bold tracking-tight'>
                  Upstream Providers
                </h1>
                <p className='text-muted-foreground mt-2 text-sm'>
                  Manage your AI provider connections and credentials
                </p>
              </div>
              <Dialog
                open={isCreateDialogOpen}
                onOpenChange={setIsCreateDialogOpen}
              >
                <DialogTrigger asChild>
                  <Button className='flex items-center gap-2'>
                    <Plus className='h-4 w-4' />
                    Add Provider
                  </Button>
                </DialogTrigger>
                <DialogContent className='sm:max-w-[500px]'>
                  <DialogHeader>
                    <DialogTitle>Add Upstream Provider</DialogTitle>
                    <DialogDescription>
                      Configure a new AI provider connection
                    </DialogDescription>
                  </DialogHeader>
                  <div className='grid gap-4 py-4'>
                    <div className='grid gap-2'>
                      <Label htmlFor='provider_type'>Provider Type</Label>
                      <Select
                        value={formData.provider_type}
                        onValueChange={(value) => {
                          setFormData({
                            ...formData,
                            provider_type: value,
                            base_url: getDefaultBaseUrl(value),
                          });
                        }}
                      >
                        <SelectTrigger>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {providerTypes.map((type) => (
                            <SelectItem key={type.id} value={type.id}>
                              {type.name}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                    <div className='grid gap-2'>
                      <Label htmlFor='base_url'>Base URL</Label>
                      <Input
                        id='base_url'
                        value={formData.base_url}
                        onChange={(e) =>
                          setFormData({ ...formData, base_url: e.target.value })
                        }
                        placeholder='https://api.example.com/v1'
                        disabled={hasFixedBaseUrl(formData.provider_type)}
                        className={
                          hasFixedBaseUrl(formData.provider_type)
                            ? 'cursor-not-allowed opacity-60'
                            : ''
                        }
                      />
                    </div>
                    <div className='grid gap-2'>
                      <Label htmlFor='api_key'>API Key</Label>
                      <Input
                        id='api_key'
                        type='password'
                        value={formData.api_key}
                        onChange={(e) =>
                          setFormData({ ...formData, api_key: e.target.value })
                        }
                        placeholder='sk-...'
                      />
                    </div>
                    {formData.provider_type === 'azure' && (
                      <div className='grid gap-2'>
                        <Label htmlFor='api_version'>API Version</Label>
                        <Input
                          id='api_version'
                          value={formData.api_version || ''}
                          onChange={(e) =>
                            setFormData({
                              ...formData,
                              api_version: e.target.value || null,
                            })
                          }
                          placeholder='2024-02-15-preview'
                        />
                      </div>
                    )}
                    <div className='flex items-center space-x-2'>
                      <Switch
                        id='enabled'
                        checked={formData.enabled}
                        onCheckedChange={(checked) =>
                          setFormData({ ...formData, enabled: checked })
                        }
                      />
                      <Label htmlFor='enabled'>Enabled</Label>
                    </div>
                  </div>
                  <DialogFooter>
                    <Button
                      variant='outline'
                      onClick={() => setIsCreateDialogOpen(false)}
                    >
                      Cancel
                    </Button>
                    <Button
                      onClick={handleCreate}
                      disabled={createMutation.isPending}
                    >
                      {createMutation.isPending ? 'Creating...' : 'Create'}
                    </Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>
            </div>

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
                <CardContent className='flex flex-col items-center justify-center py-12'>
                  <Server className='text-muted-foreground mb-4 h-12 w-12' />
                  <h3 className='mb-2 text-lg font-semibold'>
                    No providers configured
                  </h3>
                  <p className='text-muted-foreground mb-4 text-sm'>
                    Get started by adding your first upstream provider
                  </p>
                  <Button onClick={() => setIsCreateDialogOpen(true)}>
                    <Plus className='mr-2 h-4 w-4' />
                    Add Provider
                  </Button>
                </CardContent>
              </Card>
            ) : (
              <div className='grid gap-4'>
                {providers.map((provider) => (
                  <Card key={provider.id}>
                    <CardHeader>
                      <div className='flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between'>
                        <div className='min-w-0 flex-1'>
                          <div className='flex flex-col gap-2 sm:flex-row sm:items-center'>
                            <CardTitle className='truncate text-lg'>
                              {provider.provider_type}
                            </CardTitle>
                            <Badge
                              variant={
                                provider.enabled ? 'default' : 'secondary'
                              }
                              className='w-fit sm:ml-2'
                            >
                              {provider.enabled ? 'Enabled' : 'Disabled'}
                            </Badge>
                          </div>
                          <CardDescription className='mt-1 break-all'>
                            {provider.base_url}
                          </CardDescription>
                        </div>
                        <div className='grid grid-cols-3 gap-2 sm:flex sm:flex-nowrap'>
                          <Button
                            variant='outline'
                            size='sm'
                            onClick={() => toggleProviderExpansion(provider.id)}
                            className='w-full sm:w-auto'
                          >
                            <Database className='mr-1 h-4 w-4' />
                            <span className='hidden sm:inline'>Models</span>
                            {expandedProviders.has(provider.id) ? (
                              <ChevronUp className='ml-1 h-4 w-4' />
                            ) : (
                              <ChevronDown className='ml-1 h-4 w-4' />
                            )}
                          </Button>
                          <Button
                            variant='outline'
                            size='sm'
                            onClick={() => handleEdit(provider)}
                            className='w-full sm:w-auto'
                          >
                            <Pencil className='h-4 w-4' />
                          </Button>
                          <Button
                            variant='outline'
                            size='sm'
                            onClick={() => handleDelete(provider.id)}
                            className='w-full sm:w-auto'
                          >
                            <Trash2 className='h-4 w-4' />
                          </Button>
                        </div>
                      </div>
                    </CardHeader>
                    <CardContent>
                      <div className='space-y-4'>
                        <div className='space-y-2'>
                          {provider.api_version && (
                            <div className='flex items-center justify-between text-sm'>
                              <span className='text-muted-foreground'>
                                API Version:
                              </span>
                              <span className='font-mono'>
                                {provider.api_version}
                              </span>
                            </div>
                          )}
                        </div>

                        {expandedProviders.has(provider.id) && (
                          <div className='mt-4 border-t pt-4'>
                            {isLoadingModels &&
                            viewingModels === provider.id ? (
                              <div className='space-y-2'>
                                <Skeleton className='h-[40px] w-full' />
                                <Skeleton className='h-[40px] w-full' />
                              </div>
                            ) : providerModels &&
                              viewingModels === provider.id ? (
                              providerModels.remote_models.length === 0 ? (
                                // No provided models - show custom models directly without tabs
                                <div className='space-y-2'>
                                  {providerModels.db_models.length === 0 ? (
                                    <div className='text-muted-foreground py-4 text-center text-sm'>
                                      No models configured. Add custom models to
                                      use this provider.
                                    </div>
                                  ) : (
                                    <div className='space-y-2'>
                                      {providerModels.db_models.map((model) => (
                                        <div
                                          key={model.id}
                                          className='hover:bg-accent flex flex-col gap-2 rounded-lg border p-3 transition-colors sm:flex-row sm:items-center sm:justify-between'
                                        >
                                          <div className='min-w-0 flex-1'>
                                            <div className='flex flex-col gap-1 sm:flex-row sm:items-center sm:gap-2'>
                                              <span className='truncate font-mono text-sm font-medium'>
                                                {model.id}
                                              </span>
                                              <Badge
                                                variant={
                                                  model.enabled
                                                    ? 'default'
                                                    : 'secondary'
                                                }
                                                className='w-fit text-xs'
                                              >
                                                {model.enabled
                                                  ? 'Enabled'
                                                  : 'Disabled'}
                                              </Badge>
                                            </div>
                                            <div className='text-muted-foreground mt-1 text-xs break-words'>
                                              {model.description || model.name}
                                            </div>
                                          </div>
                                          <div className='text-muted-foreground text-xs whitespace-nowrap'>
                                            {model.context_length?.toLocaleString()}{' '}
                                            tokens
                                          </div>
                                        </div>
                                      ))}
                                    </div>
                                  )}
                                </div>
                              ) : (
                                // Has provided models - show tabs
                                <Tabs
                                  defaultValue='provided'
                                  className='w-full'
                                >
                                  <TabsList className='grid w-full grid-cols-2'>
                                    <TabsTrigger
                                      value='provided'
                                      className='text-xs sm:text-sm'
                                    >
                                      <span className='hidden sm:inline'>
                                        Provided Models
                                      </span>
                                      <span className='sm:hidden'>
                                        Provided
                                      </span>
                                      <Badge
                                        variant='secondary'
                                        className='ml-1 text-xs sm:ml-2'
                                      >
                                        {providerModels.remote_models.length}
                                      </Badge>
                                    </TabsTrigger>
                                    <TabsTrigger
                                      value='custom'
                                      className='text-xs sm:text-sm'
                                    >
                                      <span className='hidden sm:inline'>
                                        Custom Models
                                      </span>
                                      <span className='sm:hidden'>Custom</span>
                                      <Badge
                                        variant='secondary'
                                        className='ml-1 text-xs sm:ml-2'
                                      >
                                        {providerModels.db_models.length}
                                      </Badge>
                                    </TabsTrigger>
                                  </TabsList>
                                  <TabsContent
                                    value='custom'
                                    className='mt-4 space-y-2'
                                  >
                                    {providerModels.db_models.length > 0 && (
                                      <div className='text-muted-foreground mb-3 text-sm'>
                                        Custom models override or extend the
                                        provider&apos;s catalog.
                                      </div>
                                    )}
                                    {providerModels.db_models.length === 0 ? (
                                      <div className='text-muted-foreground py-4 text-center text-sm'>
                                        No custom models configured
                                      </div>
                                    ) : (
                                      <div className='space-y-2'>
                                        {providerModels.db_models.map(
                                          (model) => (
                                            <div
                                              key={model.id}
                                              className='hover:bg-accent flex flex-col gap-2 rounded-lg border p-3 transition-colors sm:flex-row sm:items-center sm:justify-between'
                                            >
                                              <div className='min-w-0 flex-1'>
                                                <div className='flex flex-col gap-1 sm:flex-row sm:items-center sm:gap-2'>
                                                  <span className='truncate font-mono text-sm font-medium'>
                                                    {model.id}
                                                  </span>
                                                  <Badge
                                                    variant={
                                                      model.enabled
                                                        ? 'default'
                                                        : 'secondary'
                                                    }
                                                    className='w-fit text-xs'
                                                  >
                                                    {model.enabled
                                                      ? 'Enabled'
                                                      : 'Disabled'}
                                                  </Badge>
                                                </div>
                                                <div className='text-muted-foreground mt-1 text-xs break-words'>
                                                  {model.description ||
                                                    model.name}
                                                </div>
                                              </div>
                                              <div className='text-muted-foreground text-xs whitespace-nowrap'>
                                                {model.context_length?.toLocaleString()}{' '}
                                                tokens
                                              </div>
                                            </div>
                                          )
                                        )}
                                      </div>
                                    )}
                                  </TabsContent>
                                  <TabsContent
                                    value='provided'
                                    className='mt-4 space-y-2'
                                  >
                                    {providerModels.remote_models.length >
                                      0 && (
                                      <div className='text-muted-foreground mb-3 text-sm'>
                                        Models automatically discovered from the
                                        provider&apos;s catalog.
                                      </div>
                                    )}
                                    <div className='space-y-2'>
                                      {providerModels.remote_models.map(
                                        (model) => (
                                          <div
                                            key={model.id}
                                            className='hover:bg-accent flex flex-col gap-2 rounded-lg border p-3 transition-colors sm:flex-row sm:items-center sm:justify-between'
                                          >
                                            <div className='min-w-0 flex-1'>
                                              <div className='truncate font-mono text-sm font-medium'>
                                                {model.id}
                                              </div>
                                              <div className='text-muted-foreground mt-1 text-xs break-words'>
                                                {model.description ||
                                                  model.name}
                                              </div>
                                            </div>
                                            <div className='text-muted-foreground text-xs whitespace-nowrap'>
                                              {model.context_length?.toLocaleString()}{' '}
                                              tokens
                                            </div>
                                          </div>
                                        )
                                      )}
                                    </div>
                                  </TabsContent>
                                </Tabs>
                              )
                            ) : null}
                          </div>
                        )}
                      </div>
                    </CardContent>
                  </Card>
                ))}
              </div>
            )}
          </div>
        </div>

        <Dialog open={isEditDialogOpen} onOpenChange={setIsEditDialogOpen}>
          <DialogContent className='sm:max-w-[500px]'>
            <DialogHeader>
              <DialogTitle>Edit Upstream Provider</DialogTitle>
              <DialogDescription>
                Update provider configuration
              </DialogDescription>
            </DialogHeader>
            <div className='grid gap-4 py-4'>
              <div className='grid gap-2'>
                <Label htmlFor='edit_provider_type'>Provider Type</Label>
                <Select
                  value={formData.provider_type}
                  onValueChange={(value) => {
                    setFormData({
                      ...formData,
                      provider_type: value,
                      base_url: getDefaultBaseUrl(value),
                    });
                  }}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {providerTypes.map((type) => (
                      <SelectItem key={type.id} value={type.id}>
                        {type.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className='grid gap-2'>
                <Label htmlFor='edit_base_url'>Base URL</Label>
                <Input
                  id='edit_base_url'
                  value={formData.base_url}
                  onChange={(e) =>
                    setFormData({ ...formData, base_url: e.target.value })
                  }
                  placeholder='https://api.example.com/v1'
                  disabled={hasFixedBaseUrl(formData.provider_type)}
                  className={
                    hasFixedBaseUrl(formData.provider_type)
                      ? 'cursor-not-allowed opacity-60'
                      : ''
                  }
                />
              </div>
              <div className='grid gap-2'>
                <Label htmlFor='edit_api_key'>
                  API Key (leave blank to keep current)
                </Label>
                <Input
                  id='edit_api_key'
                  type='password'
                  value={formData.api_key}
                  onChange={(e) =>
                    setFormData({ ...formData, api_key: e.target.value })
                  }
                  placeholder='Leave blank to keep current'
                />
              </div>
              {formData.provider_type === 'azure' && (
                <div className='grid gap-2'>
                  <Label htmlFor='edit_api_version'>API Version</Label>
                  <Input
                    id='edit_api_version'
                    value={formData.api_version || ''}
                    onChange={(e) =>
                      setFormData({
                        ...formData,
                        api_version: e.target.value || null,
                      })
                    }
                    placeholder='2024-02-15-preview'
                  />
                </div>
              )}
              <div className='flex items-center space-x-2'>
                <Switch
                  id='edit_enabled'
                  checked={formData.enabled}
                  onCheckedChange={(checked) =>
                    setFormData({ ...formData, enabled: checked })
                  }
                />
                <Label htmlFor='edit_enabled'>Enabled</Label>
              </div>
            </div>
            <DialogFooter>
              <Button
                variant='outline'
                onClick={() => setIsEditDialogOpen(false)}
              >
                Cancel
              </Button>
              <Button
                onClick={handleUpdate}
                disabled={updateMutation.isPending}
              >
                {updateMutation.isPending ? 'Updating...' : 'Update'}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </SidebarInset>
    </SidebarProvider>
  );
}
