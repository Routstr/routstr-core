'use client';

import React, { useState, useEffect, useCallback } from 'react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { AdminService, AdminModel } from '@/lib/api/services/admin';
import { toast } from 'sonner';
import { Download, AlertCircle, CheckCircle, Loader2 } from 'lucide-react';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Checkbox } from '@/components/ui/checkbox';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Badge } from '@/components/ui/badge';

interface CollectModelsDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess?: () => void;
}

export function CollectModelsDialog({
  isOpen,
  onClose,
  onSuccess,
}: CollectModelsDialogProps) {
  const [selectedProvider, setSelectedProvider] = useState<number | null>(null);
  const [providers, setProviders] = useState<
    Array<{ id: number; provider_type: string; base_url: string }>
  >([]);
  const [remoteModels, setRemoteModels] = useState<AdminModel[]>([]);
  const [selectedModels, setSelectedModels] = useState<Set<string>>(new Set());
  const [isLoadingProviders, setIsLoadingProviders] = useState(false);
  const [isLoadingModels, setIsLoadingModels] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [result, setResult] = useState<{
    added: number;
    skipped: number;
    errors: string[];
  } | null>(null);

  const loadRemoteModels = useCallback(async () => {
    if (!selectedProvider) return;

    setIsLoadingModels(true);
    setRemoteModels([]);
    setSelectedModels(new Set());

    try {
      const data = await AdminService.getProviderModels(selectedProvider);
      const dbModelIds = new Set(data.db_models.map((m) => m.id));
      const availableRemoteModels = data.remote_models.filter(
        (m: AdminModel) => !dbModelIds.has(m.id)
      );
      setRemoteModels(availableRemoteModels);
    } catch {
      toast.error('Failed to fetch models from provider');
    } finally {
      setIsLoadingModels(false);
    }
  }, [selectedProvider]);

  useEffect(() => {
    if (isOpen) {
      loadProviders();
    }
  }, [isOpen]);

  useEffect(() => {
    if (selectedProvider) {
      loadRemoteModels();
    }
  }, [selectedProvider, loadRemoteModels]);

  const loadProviders = async () => {
    setIsLoadingProviders(true);
    try {
      const data = await AdminService.getUpstreamProviders();
      setProviders(data);
    } catch {
      toast.error('Failed to load providers');
    } finally {
      setIsLoadingProviders(false);
    }
  };

  const toggleModel = (modelId: string) => {
    const newSelected = new Set(selectedModels);
    if (newSelected.has(modelId)) {
      newSelected.delete(modelId);
    } else {
      newSelected.add(modelId);
    }
    setSelectedModels(newSelected);
  };

  const selectAll = () => {
    setSelectedModels(new Set(remoteModels.map((m) => m.id)));
  };

  const deselectAll = () => {
    setSelectedModels(new Set());
  };

  const handleCollect = async () => {
    if (selectedModels.size === 0) {
      toast.error('Please select at least one model');
      return;
    }

    setIsSubmitting(true);
    let added = 0;
    let skipped = 0;
    const errors: string[] = [];

    try {
      for (const modelId of Array.from(selectedModels)) {
        const model = remoteModels.find((m) => m.id === modelId);
        if (!model) continue;

        try {
          await AdminService.createModel({
            ...model,
            upstream_provider_id: selectedProvider,
            enabled: true,
            created: Math.floor(Date.now() / 1000),
          });
          added++;
        } catch (err) {
          const errorMessage =
            err instanceof Error ? err.message : 'Unknown error';
          if (errorMessage.includes('already exists')) {
            skipped++;
          } else {
            errors.push(`${modelId}: ${errorMessage}`);
          }
        }
      }

      setResult({ added, skipped, errors });

      if (added > 0) {
        toast.success(`Successfully added ${added} models`);
        onSuccess?.();
      }

      if (errors.length > 0) {
        toast.warning(`Completed with ${errors.length} errors`);
      }
    } catch {
      toast.error('Failed to collect models');
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleClose = () => {
    if (!isSubmitting) {
      setSelectedProvider(null);
      setRemoteModels([]);
      setSelectedModels(new Set());
      setResult(null);
      onClose();
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={handleClose}>
      <DialogContent className='max-h-[80vh] sm:max-w-[700px]'>
        <DialogHeader>
          <DialogTitle className='flex items-center gap-2'>
            <Download className='h-5 w-5' />
            Collect Models from Provider
          </DialogTitle>
          <DialogDescription>
            Fetch models from an upstream provider and add them to your database
          </DialogDescription>
        </DialogHeader>

        <div className='space-y-4'>
          <div className='space-y-2'>
            <label className='text-sm font-medium'>Select Provider</label>
            <Select
              value={selectedProvider?.toString()}
              onValueChange={(value) => setSelectedProvider(parseInt(value))}
              disabled={isLoadingProviders}
            >
              <SelectTrigger>
                <SelectValue placeholder='Choose an upstream provider' />
              </SelectTrigger>
              <SelectContent>
                {providers.map((provider) => (
                  <SelectItem key={provider.id} value={provider.id.toString()}>
                    {provider.provider_type} - {provider.base_url}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {isLoadingModels && (
            <div className='flex items-center justify-center py-8'>
              <Loader2 className='h-8 w-8 animate-spin' />
              <span className='ml-2'>Loading models from provider...</span>
            </div>
          )}

          {!isLoadingModels && selectedProvider && remoteModels.length > 0 && (
            <>
              <div className='flex items-center justify-between'>
                <div className='text-sm font-medium'>
                  {remoteModels.length} models available
                </div>
                <div className='flex gap-2'>
                  <Button
                    variant='outline'
                    size='sm'
                    onClick={selectAll}
                    disabled={isSubmitting}
                  >
                    Select All
                  </Button>
                  <Button
                    variant='outline'
                    size='sm'
                    onClick={deselectAll}
                    disabled={isSubmitting}
                  >
                    Deselect All
                  </Button>
                </div>
              </div>

              <ScrollArea className='h-[300px] rounded-md border p-4'>
                <div className='space-y-2'>
                  {remoteModels.map((model) => (
                    <div
                      key={model.id}
                      className='hover:bg-accent flex items-start space-x-3 rounded-lg border p-3'
                    >
                      <Checkbox
                        checked={selectedModels.has(model.id)}
                        onCheckedChange={() => toggleModel(model.id)}
                        disabled={isSubmitting}
                      />
                      <div className='flex-1 space-y-1'>
                        <div className='font-mono text-sm font-medium'>
                          {model.id}
                        </div>
                        <div className='text-muted-foreground text-xs'>
                          {model.description || model.name}
                        </div>
                        {model.context_length && (
                          <Badge variant='secondary' className='text-xs'>
                            {model.context_length.toLocaleString()} tokens
                          </Badge>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </ScrollArea>
            </>
          )}

          {!isLoadingModels &&
            selectedProvider &&
            remoteModels.length === 0 && (
              <Alert>
                <AlertCircle className='h-4 w-4' />
                <AlertDescription>
                  No new models available from this provider. All models may
                  already be in your database.
                </AlertDescription>
              </Alert>
            )}

          {result && (
            <div className='space-y-2'>
              <Alert>
                <CheckCircle className='h-4 w-4' />
                <AlertDescription>
                  <strong>Collection Results:</strong>
                  <br />• {result.added} models added
                  <br />• {result.skipped} models skipped
                  {result.errors.length > 0 && (
                    <>
                      <br />• {result.errors.length} errors occurred
                    </>
                  )}
                </AlertDescription>
              </Alert>

              {result.errors.length > 0 && (
                <Alert variant='destructive'>
                  <AlertCircle className='h-4 w-4' />
                  <AlertDescription>
                    <strong>Errors:</strong>
                    <ul className='mt-1 list-inside list-disc text-sm'>
                      {result.errors.slice(0, 3).map((error, index) => (
                        <li key={index}>{error}</li>
                      ))}
                      {result.errors.length > 3 && (
                        <li>... and {result.errors.length - 3} more errors</li>
                      )}
                    </ul>
                  </AlertDescription>
                </Alert>
              )}
            </div>
          )}

          <div className='flex justify-end gap-2 pt-4'>
            <Button
              variant='outline'
              onClick={handleClose}
              disabled={isSubmitting}
            >
              {result ? 'Close' : 'Cancel'}
            </Button>
            <Button
              onClick={handleCollect}
              disabled={
                isSubmitting ||
                !selectedProvider ||
                selectedModels.size === 0 ||
                isLoadingModels
              }
            >
              {isSubmitting ? (
                <>
                  <Loader2 className='mr-2 h-4 w-4 animate-spin' />
                  Adding Models...
                </>
              ) : (
                <>
                  <Download className='mr-2 h-4 w-4' />
                  Add {selectedModels.size} Models
                </>
              )}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
