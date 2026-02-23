'use client';

import React, { useState, useEffect, useCallback } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { type Model } from '@/lib/api/schemas/models';
import { AdminService, type AdminModel } from '@/lib/api/services/admin';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  Form,
  FormControl,
  FormDescription,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from '@/components/ui/form';
import { Edit3, Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { Switch } from '@/components/ui/switch';

const EditModelFormSchema = z.object({
  name: z.string().min(1, 'Name is required'),
  description: z.string().optional(),
  context_length: z.number().min(0),
  prompt: z.number().min(0),
  completion: z.number().min(0),
  enabled: z.boolean(),
});

type EditModelFormData = z.infer<typeof EditModelFormSchema>;

const roundToFiveDecimals = (value: number | undefined | null): number => {
  if (value === undefined || value === null || isNaN(value)) {
    return 0;
  }
  return Math.round(value * 100000) / 100000;
};

const toNumber = (value: unknown, fallback = 0): number => {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === 'string') {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
  }
  return fallback;
};

const toStringArray = (value: unknown, fallback: string[]): string[] => {
  if (!Array.isArray(value)) {
    return fallback;
  }
  const filtered = value.filter(
    (item): item is string => typeof item === 'string'
  );
  return filtered.length > 0 ? filtered : fallback;
};

interface EditModelFormProps {
  model: Model;
  providerId?: number;
  onModelUpdate?: () => void;
  onCancel?: () => void;
  isOpen: boolean;
}

interface AdminModelData {
  id: string;
  name: string;
  description?: string;
  created: number;
  context_length: number;
  architecture: {
    modality: string;
    input_modalities: string[];
    output_modalities: string[];
    tokenizer: string;
    instruct_type: string | null;
  };
  pricing: {
    prompt: number;
    completion: number;
    request: number;
    image: number;
    web_search: number;
    internal_reasoning: number;
  };
  per_request_limits: null | undefined;
  top_provider: null | undefined;
  upstream_provider_id: number;
  enabled: boolean;
}

const normalizeAdminModelData = (
  adminModel: AdminModel,
  fallbackModel: Model,
  providerId: number
): AdminModelData => {
  const pricingRecord =
    adminModel.pricing && typeof adminModel.pricing === 'object'
      ? (adminModel.pricing as Record<string, unknown>)
      : {};

  const architectureRecord =
    adminModel.architecture && typeof adminModel.architecture === 'object'
      ? (adminModel.architecture as Record<string, unknown>)
      : {};

  return {
    id: adminModel.id,
    name: adminModel.name,
    description: adminModel.description || '',
    created: toNumber(adminModel.created, Math.floor(Date.now() / 1000)),
    context_length: Math.max(
      0,
      Math.trunc(
        toNumber(adminModel.context_length, fallbackModel.contextLength || 4096)
      )
    ),
    architecture: {
      modality:
        typeof architectureRecord.modality === 'string'
          ? architectureRecord.modality
          : fallbackModel.modelType || 'text',
      input_modalities: toStringArray(architectureRecord.input_modalities, [
        fallbackModel.modelType || 'text',
      ]),
      output_modalities: toStringArray(architectureRecord.output_modalities, [
        fallbackModel.modelType || 'text',
      ]),
      tokenizer:
        typeof architectureRecord.tokenizer === 'string'
          ? architectureRecord.tokenizer
          : '',
      instruct_type:
        typeof architectureRecord.instruct_type === 'string'
          ? architectureRecord.instruct_type
          : null,
    },
    pricing: {
      prompt: roundToFiveDecimals(
        toNumber(pricingRecord.prompt, fallbackModel.input_cost)
      ),
      completion: roundToFiveDecimals(
        toNumber(pricingRecord.completion, fallbackModel.output_cost)
      ),
      request: toNumber(pricingRecord.request, 0),
      image: toNumber(pricingRecord.image, 0),
      web_search: toNumber(pricingRecord.web_search, 0),
      internal_reasoning: toNumber(pricingRecord.internal_reasoning, 0),
    },
    per_request_limits:
      adminModel.per_request_limits === null ||
      adminModel.per_request_limits === undefined
        ? adminModel.per_request_limits
        : null,
    top_provider:
      adminModel.top_provider === null || adminModel.top_provider === undefined
        ? adminModel.top_provider
        : null,
    upstream_provider_id:
      typeof adminModel.upstream_provider_id === 'number'
        ? adminModel.upstream_provider_id
        : providerId,
    enabled: adminModel.enabled !== false,
  };
};

export function EditModelForm({
  model,
  providerId,
  onModelUpdate,
  onCancel,
  isOpen,
}: EditModelFormProps) {
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [adminModelData, setAdminModelData] = useState<AdminModelData | null>(
    null
  );
  const [isNewOverride, setIsNewOverride] = useState(false);

  const form = useForm<EditModelFormData>({
    resolver: zodResolver(EditModelFormSchema),
    defaultValues: {
      name: model.name,
      description: model.description || '',
      context_length: model.contextLength || 4096,
      prompt: roundToFiveDecimals(model.input_cost),
      completion: roundToFiveDecimals(model.output_cost),
      enabled: model.isEnabled !== false,
    },
  });

  const loadAdminModel = useCallback(async () => {
    if (!providerId) {
      console.error('loadAdminModel called without providerId');
      return;
    }

    try {
      const adminModel = await AdminService.getProviderModel(
        providerId,
        model.id
      );

      const normalizedAdminModel = normalizeAdminModelData(
        adminModel,
        model,
        providerId
      );
      setAdminModelData(normalizedAdminModel);
      setIsNewOverride(false);

      form.reset({
        name: normalizedAdminModel.name,
        description: normalizedAdminModel.description || '',
        context_length: normalizedAdminModel.context_length,
        prompt: normalizedAdminModel.pricing.prompt,
        completion: normalizedAdminModel.pricing.completion,
        enabled: normalizedAdminModel.enabled !== false,
      });
    } catch {
      setIsNewOverride(true);
      setAdminModelData({
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
          prompt: roundToFiveDecimals(model.input_cost),
          completion: roundToFiveDecimals(model.output_cost),
          request: 0,
          image: 0,
          web_search: 0,
          internal_reasoning: 0,
        },
        per_request_limits: null,
        top_provider: null,
        upstream_provider_id: providerId,
        enabled: model.isEnabled !== false,
      });

      form.reset({
        name: model.name,
        description: model.description || '',
        context_length: model.contextLength || 4096,
        prompt: roundToFiveDecimals(model.input_cost),
        completion: roundToFiveDecimals(model.output_cost),
        enabled: model.isEnabled !== false,
      });
    }
  }, [providerId, model, form]);

  useEffect(() => {
    if (isOpen && providerId) {
      loadAdminModel();
    } else if (isOpen && !providerId) {
      console.error('EditModelForm opened without providerId', {
        model,
        providerId,
      });
      toast.error('Missing provider information for this model');
    }
  }, [isOpen, providerId, model, loadAdminModel]);

  const onSubmit = async (data: EditModelFormData) => {
    if (!providerId) {
      console.error('onSubmit called without providerId', {
        model,
        providerId,
      });
      toast.error('Missing provider ID - cannot update model');
      return;
    }

    if (!adminModelData) {
      console.error('onSubmit called without adminModelData', {
        model,
        providerId,
        adminModelData,
      });
      toast.error('Model data not loaded - please try reopening the form');
      return;
    }

    setIsSubmitting(true);
    try {
      const payload = {
        id: adminModelData.id,
        name: data.name,
        description: data.description || '',
        created: adminModelData.created || Math.floor(Date.now() / 1000),
        context_length: data.context_length,
        architecture: adminModelData.architecture || {
          modality: 'text',
          input_modalities: ['text'],
          output_modalities: ['text'],
          tokenizer: '',
          instruct_type: null,
        },
        pricing: {
          prompt: roundToFiveDecimals(data.prompt),
          completion: roundToFiveDecimals(data.completion),
          request: 0,
          image: 0,
          web_search: 0,
          internal_reasoning: 0,
        },
        per_request_limits: adminModelData.per_request_limits,
        top_provider: adminModelData.top_provider,
        upstream_provider_id: providerId,
        enabled: data.enabled,
      };

      if (isNewOverride) {
        await AdminService.createProviderModel(providerId, payload);
        toast.success('Model override created successfully!');
      } else {
        await AdminService.updateProviderModel(
          providerId,
          adminModelData.id,
          payload
        );
        toast.success('Model updated successfully!');
      }

      onModelUpdate?.();
      onCancel?.();
    } catch (error) {
      const action = isNewOverride ? 'create' : 'update';
      toast.error(`Failed to ${action} model. Please try again.`);
      console.error(`Error ${action}ing model:`, error);
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleClose = () => {
    if (!isSubmitting) {
      onCancel?.();
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={handleClose}>
      <DialogContent className='max-h-[90vh] overflow-y-auto sm:max-w-[600px]'>
        <DialogHeader>
          <DialogTitle className='flex items-center gap-2'>
            <Edit3 className='h-5 w-5' />
            {isNewOverride ? 'Create Model Override' : 'Edit Model Override'}
          </DialogTitle>
          <DialogDescription>
            {isNewOverride
              ? `Create an override for &quot;${model.name}&quot;`
              : `Update the model override for &quot;${model.name}&quot;`}
          </DialogDescription>
        </DialogHeader>

        <Form {...form}>
          <form onSubmit={form.handleSubmit(onSubmit)} className='space-y-4'>
            <div className='grid grid-cols-1 gap-4 sm:grid-cols-2'>
              <FormField
                control={form.control}
                name='name'
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Display Name *</FormLabel>
                    <FormControl>
                      <Input
                        placeholder='e.g., GPT-4'
                        {...field}
                        className='w-full'
                      />
                    </FormControl>
                    <FormDescription>
                      Custom display name for the model
                    </FormDescription>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name='context_length'
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Context Length *</FormLabel>
                    <FormControl>
                      <Input
                        type='number'
                        min='0'
                        placeholder='4096'
                        value={field.value ?? ''}
                        onChange={(e) => {
                          const value = e.target.value;
                          field.onChange(
                            value === '' ? 0 : parseInt(value, 10) || 0
                          );
                        }}
                        onBlur={(e) => {
                          const value = parseInt(e.target.value, 10);
                          field.onChange(
                            Number.isNaN(value) ? 0 : Math.max(0, value)
                          );
                        }}
                        className='w-full'
                      />
                    </FormControl>
                    <FormDescription>
                      Maximum context window size
                    </FormDescription>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>

            <FormField
              control={form.control}
              name='description'
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Description</FormLabel>
                  <FormControl>
                    <Textarea
                      placeholder='Brief description of the model...'
                      {...field}
                      rows={3}
                      className='w-full'
                    />
                  </FormControl>
                  <FormDescription>
                    Optional description or notes about the model
                  </FormDescription>
                  <FormMessage />
                </FormItem>
              )}
            />

            <div className='grid grid-cols-1 gap-4 sm:grid-cols-2'>
              <FormField
                control={form.control}
                name='prompt'
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Input Cost (per 1M tokens) *</FormLabel>
                    <FormControl>
                      <Input
                        type='number'
                        step='0.00001'
                        min='0'
                        placeholder='5.00000'
                        value={field.value ?? ''}
                        onChange={(e) => {
                          const value = e.target.value;
                          field.onChange(value === '' ? 0 : parseFloat(value));
                        }}
                        onBlur={(e) => {
                          const value = parseFloat(e.target.value);
                          field.onChange(roundToFiveDecimals(value));
                        }}
                        className='w-full'
                      />
                    </FormControl>
                    <FormDescription>
                      Cost in USD per 1,000,000 input tokens (max 5 decimals)
                    </FormDescription>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name='completion'
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Output Cost (per 1M tokens) *</FormLabel>
                    <FormControl>
                      <Input
                        type='number'
                        step='0.00001'
                        min='0'
                        placeholder='15.00000'
                        value={field.value ?? ''}
                        onChange={(e) => {
                          const value = e.target.value;
                          field.onChange(value === '' ? 0 : parseFloat(value));
                        }}
                        onBlur={(e) => {
                          const value = parseFloat(e.target.value);
                          field.onChange(roundToFiveDecimals(value));
                        }}
                        className='w-full'
                      />
                    </FormControl>
                    <FormDescription>
                      Cost in USD per 1,000,000 output tokens (max 5 decimals)
                    </FormDescription>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>

            <FormField
              control={form.control}
              name='enabled'
              render={({ field }) => (
                <FormItem className='flex flex-row items-center justify-between rounded-lg border p-4'>
                  <div className='space-y-0.5'>
                    <FormLabel className='text-base'>Model Enabled</FormLabel>
                    <FormDescription>
                      Enable or disable this model override
                    </FormDescription>
                  </div>
                  <FormControl>
                    <Switch
                      checked={field.value}
                      onCheckedChange={field.onChange}
                    />
                  </FormControl>
                </FormItem>
              )}
            />

            <div className='flex justify-end gap-2 pt-4'>
              <Button
                type='button'
                variant='outline'
                onClick={handleClose}
                disabled={isSubmitting}
              >
                Cancel
              </Button>
              <Button type='submit' disabled={isSubmitting}>
                {isSubmitting ? (
                  <>
                    <Loader2 className='mr-2 h-4 w-4 animate-spin' />
                    {isNewOverride ? 'Creating...' : 'Updating...'}
                  </>
                ) : isNewOverride ? (
                  'Create Override'
                ) : (
                  'Update Model'
                )}
              </Button>
            </div>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  );
}
