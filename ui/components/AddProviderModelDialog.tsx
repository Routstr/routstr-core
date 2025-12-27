'use client';

import React, { useEffect, useMemo, useState } from 'react';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { zodResolver } from '@hookform/resolvers/zod';
import { useQuery } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';
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
  DialogFooter,
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
import { Switch } from '@/components/ui/switch';
import { Loader2, Plus } from 'lucide-react';
import { toast } from 'sonner';
import { AdminService, type AdminModel } from '@/lib/api/services/admin';

const listFromString = (value: string): string[] =>
  value
    .split(',')
    .map((item) => item.trim())
    .filter((item) => item.length > 0);

const listToString = (value: string[] | undefined | null): string =>
  value && value.length > 0 ? value.join(', ') : '';

const FormSchema = z.object({
  id: z.string().min(1, 'Model ID is required'),
  name: z.string().min(1, 'Name is required'),
  description: z.string().default(''),
  context_length: z.coerce.number().min(0).default(8192),
  modality: z.string().min(1, 'Modality is required'),
  input_modalities_raw: z.string().default(''),
  output_modalities_raw: z.string().default(''),
  tokenizer: z.string().default(''),
  instruct_type: z.string().default(''),
  canonical_slug: z.string().default(''),
  alias_ids_raw: z.string().default(''),
  upstream_provider_id: z.string().default(''),
  input_cost: z.coerce.number().min(0).default(0),
  output_cost: z.coerce.number().min(0).default(0),
  request_cost: z.coerce.number().min(0).default(0),
  image_cost: z.coerce.number().min(0).default(0),
  web_search_cost: z.coerce.number().min(0).default(0),
  internal_reasoning_cost: z.coerce.number().min(0).default(0),
  max_prompt_cost: z.coerce.number().min(0).default(0),
  max_completion_cost: z.coerce.number().min(0).default(0),
  max_cost: z.coerce.number().min(0).default(0),
  per_request_limits_raw: z.string().default(''),
  top_provider_context_length: z.coerce.number().min(0).optional(),
  top_provider_max_completion_tokens: z.coerce.number().min(0).optional(),
  top_provider_is_moderated: z.boolean().default(false),
  enabled: z.boolean().default(true),
});

type FormData = z.output<typeof FormSchema>;

export interface AddProviderModelDialogProps {
  providerId: number;
  isOpen: boolean;
  onClose: () => void;
  onSuccess: () => void;
  initialData?: AdminModel | null;
  mode?: 'create' | 'edit' | 'override';
}

export function AddProviderModelDialog({
  providerId,
  isOpen,
  onClose,
  onSuccess,
  initialData,
  mode = 'create',
}: AddProviderModelDialogProps) {
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isPresetOpen, setIsPresetOpen] = useState(false);
  const [selectedPresetLabel, setSelectedPresetLabel] =
    useState('Select a preset');

  const form = useForm<FormData>({
    resolver: zodResolver(FormSchema) as never,
    defaultValues: {
      id: '',
      name: '',
      description: '',
      context_length: 8192,
      modality: 'text',
      input_modalities_raw: 'text',
      output_modalities_raw: 'text',
      tokenizer: '',
      instruct_type: '',
      canonical_slug: '',
      alias_ids_raw: '',
      upstream_provider_id: '',
      input_cost: 0,
      output_cost: 0,
      request_cost: 0,
      image_cost: 0,
      web_search_cost: 0,
      internal_reasoning_cost: 0,
      max_prompt_cost: 0,
      max_completion_cost: 0,
      max_cost: 0,
      per_request_limits_raw: '',
      top_provider_context_length: undefined,
      top_provider_max_completion_tokens: undefined,
      top_provider_is_moderated: false,
      enabled: true,
    },
  });

  const isOverride = useMemo(() => mode === 'override', [mode]);
  const isEdit = useMemo(() => mode === 'edit', [mode]);
  const { data: presets = [], isLoading: isLoadingPresets } = useQuery({
    queryKey: ['openrouter-presets'],
    queryFn: () => AdminService.getOpenRouterPresets(),
    staleTime: 10 * 60 * 1000,
    refetchOnWindowFocus: false,
  });

  useEffect(() => {
    if (initialData) {
      const architecture = initialData.architecture as Record<string, unknown>;
      const pricing = initialData.pricing as Record<string, number>;
      const topProvider = initialData.top_provider as Record<
        string,
        unknown
      > | null;

      form.reset({
        id: initialData.id,
        name: initialData.name,
        description: initialData.description,
        context_length: initialData.context_length,
        modality:
          typeof architecture?.modality === 'string'
            ? architecture.modality
            : 'text',
        input_modalities_raw: listToString(
          (architecture?.input_modalities as string[]) || []
        ),
        output_modalities_raw: listToString(
          (architecture?.output_modalities as string[]) || []
        ),
        tokenizer:
          typeof architecture?.tokenizer === 'string'
            ? architecture.tokenizer
            : '',
        instruct_type:
          typeof architecture?.instruct_type === 'string'
            ? architecture.instruct_type
            : '',
        canonical_slug: initialData.canonical_slug || '',
        alias_ids_raw: listToString(initialData.alias_ids),
        upstream_provider_id:
          typeof initialData.upstream_provider_id === 'string'
            ? initialData.upstream_provider_id
            : initialData.upstream_provider_id?.toString() || '',
        input_cost: pricing?.prompt ?? 0,
        output_cost: pricing?.completion ?? 0,
        request_cost: pricing?.request ?? 0,
        image_cost: pricing?.image ?? 0,
        web_search_cost: pricing?.web_search ?? 0,
        internal_reasoning_cost: pricing?.internal_reasoning ?? 0,
        max_prompt_cost: pricing?.max_prompt_cost ?? 0,
        max_completion_cost: pricing?.max_completion_cost ?? 0,
        max_cost: pricing?.max_cost ?? 0,
        per_request_limits_raw: initialData.per_request_limits
          ? JSON.stringify(initialData.per_request_limits, null, 2)
          : '',
        top_provider_context_length:
          typeof topProvider?.context_length === 'number'
            ? topProvider.context_length
            : undefined,
        top_provider_max_completion_tokens:
          typeof topProvider?.max_completion_tokens === 'number'
            ? topProvider.max_completion_tokens
            : undefined,
        top_provider_is_moderated:
          typeof topProvider?.is_moderated === 'boolean'
            ? topProvider.is_moderated
            : false,
        enabled: initialData.enabled,
      });
    } else {
      form.reset({
        id: '',
        name: '',
        description: '',
        context_length: 8192,
        modality: 'text',
        input_modalities_raw: 'text',
        output_modalities_raw: 'text',
        tokenizer: '',
        instruct_type: '',
        canonical_slug: '',
        alias_ids_raw: '',
        upstream_provider_id: '',
        input_cost: 0,
        output_cost: 0,
        request_cost: 0,
        image_cost: 0,
        web_search_cost: 0,
        internal_reasoning_cost: 0,
        max_prompt_cost: 0,
        max_completion_cost: 0,
        max_cost: 0,
        per_request_limits_raw: '',
        top_provider_context_length: undefined,
        top_provider_max_completion_tokens: undefined,
        top_provider_is_moderated: false,
        enabled: true,
      });
    }
  }, [initialData, form, isOpen]);

  const applyModelToForm = (model: AdminModel) => {
    setSelectedPresetLabel(`${model.id} — ${model.name}`);
    const architecture = model.architecture as Record<string, unknown>;
    const pricing = model.pricing as Record<string, number>;
    const topProvider = model.top_provider as Record<string, unknown> | null;

    form.setValue('id', model.id);
    form.setValue('name', model.name);
    form.setValue('description', model.description || '');
    form.setValue('context_length', model.context_length);
    form.setValue(
      'modality',
      typeof architecture?.modality === 'string'
        ? architecture.modality
        : 'text'
    );
    form.setValue(
      'input_modalities_raw',
      listToString((architecture?.input_modalities as string[]) || [])
    );
    form.setValue(
      'output_modalities_raw',
      listToString((architecture?.output_modalities as string[]) || [])
    );
    form.setValue(
      'tokenizer',
      typeof architecture?.tokenizer === 'string' ? architecture.tokenizer : ''
    );
    form.setValue(
      'instruct_type',
      typeof architecture?.instruct_type === 'string'
        ? architecture.instruct_type
        : ''
    );
    form.setValue('canonical_slug', model.canonical_slug || '');
    form.setValue('alias_ids_raw', listToString(model.alias_ids));
    form.setValue(
      'upstream_provider_id',
      typeof model.upstream_provider_id === 'string'
        ? model.upstream_provider_id
        : model.upstream_provider_id?.toString() || ''
    );
    form.setValue('input_cost', pricing?.prompt ?? 0);
    form.setValue('output_cost', pricing?.completion ?? 0);
    form.setValue('request_cost', pricing?.request ?? 0);
    form.setValue('image_cost', pricing?.image ?? 0);
    form.setValue('web_search_cost', pricing?.web_search ?? 0);
    form.setValue('internal_reasoning_cost', pricing?.internal_reasoning ?? 0);
    form.setValue('max_prompt_cost', pricing?.max_prompt_cost ?? 0);
    form.setValue('max_completion_cost', pricing?.max_completion_cost ?? 0);
    form.setValue('max_cost', pricing?.max_cost ?? 0);
    form.setValue(
      'per_request_limits_raw',
      model.per_request_limits
        ? JSON.stringify(model.per_request_limits, null, 2)
        : ''
    );
    form.setValue(
      'top_provider_context_length',
      typeof topProvider?.context_length === 'number'
        ? topProvider.context_length
        : undefined
    );
    form.setValue(
      'top_provider_max_completion_tokens',
      typeof topProvider?.max_completion_tokens === 'number'
        ? topProvider.max_completion_tokens
        : undefined
    );
    form.setValue(
      'top_provider_is_moderated',
      typeof topProvider?.is_moderated === 'boolean'
        ? topProvider.is_moderated
        : false
    );
    form.setValue('enabled', model.enabled);
  };

  const onSubmit = async (data: FormData) => {
    setIsSubmitting(true);
    try {
      let perRequestLimits: Record<string, unknown> | null = null;
      if (
        data.per_request_limits_raw &&
        data.per_request_limits_raw.trim().length
      ) {
        try {
          perRequestLimits = JSON.parse(data.per_request_limits_raw);
        } catch {
          toast.error('Per-request limits must be valid JSON');
          setIsSubmitting(false);
          return;
        }
      }

      const adminModel: AdminModel = {
        id: data.id,
        name: data.name,
        description: data.description || '',
        created: Math.floor(Date.now() / 1000),
        context_length: data.context_length,
        architecture: {
          modality: data.modality,
          input_modalities: listFromString(
            data.input_modalities_raw || data.modality
          ),
          output_modalities: listFromString(
            data.output_modalities_raw || data.modality
          ),
          tokenizer: data.tokenizer || '',
          instruct_type: data.instruct_type?.trim() || null,
        },
        pricing: {
          prompt: data.input_cost,
          completion: data.output_cost,
          request: data.request_cost,
          image: data.image_cost,
          web_search: data.web_search_cost,
          internal_reasoning: data.internal_reasoning_cost,
          max_prompt_cost: data.max_prompt_cost,
          max_completion_cost: data.max_completion_cost,
          max_cost: data.max_cost,
        },
        per_request_limits: perRequestLimits,
        top_provider:
          data.top_provider_context_length ||
          data.top_provider_max_completion_tokens ||
          data.top_provider_is_moderated
            ? {
                context_length: data.top_provider_context_length ?? null,
                max_completion_tokens:
                  data.top_provider_max_completion_tokens ?? null,
                is_moderated: data.top_provider_is_moderated,
              }
            : null,
        upstream_provider_id: data.upstream_provider_id?.trim().length
          ? data.upstream_provider_id.trim()
          : providerId,
        canonical_slug: data.canonical_slug?.trim() || null,
        alias_ids: listFromString(data.alias_ids_raw || ''),
        enabled: data.enabled,
      };

      if (isEdit) {
        await AdminService.updateProviderModel(providerId, data.id, adminModel);
        toast.success('Model updated successfully');
      } else {
        await AdminService.createProviderModel(providerId, adminModel);
        toast.success(
          isOverride ? 'Model override created' : 'Model created successfully'
        );
      }

      onSuccess();
      onClose();
    } catch (error: unknown) {
      const message =
        error instanceof Error ? error.message : 'Unknown error saving model';
      toast.error(`Failed to save model: ${message}`);
    } finally {
      setIsSubmitting(false);
    }
  };

  const title = isEdit
    ? 'Edit Model'
    : isOverride
      ? 'Override Model'
      : 'Add Custom Model';
  const description = isOverride
    ? 'Create a custom override for this upstream model'
    : 'Add a new model configuration for this provider';

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className='max-h-[90vh] overflow-y-auto sm:max-w-[720px]'>
        <DialogHeader>
          <DialogTitle className='flex items-center gap-2'>
            <Plus className='h-4 w-4' />
            {title}
          </DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        {!isEdit && !isOverride && (
          <div className='bg-muted/30 rounded-md border p-3'>
            <div className='mb-2 text-sm font-medium'>Presets</div>
            <div className='grid gap-2 sm:grid-cols-3 sm:items-start'>
              <Popover open={isPresetOpen} onOpenChange={setIsPresetOpen}>
                <PopoverTrigger asChild>
                  <Button
                    variant='outline'
                    role='combobox'
                    aria-expanded={isPresetOpen}
                    className='w-full justify-between overflow-hidden text-left text-sm'
                  >
                    <span className='truncate'>
                      {isLoadingPresets
                        ? 'Loading presets...'
                        : selectedPresetLabel}
                    </span>
                  </Button>
                </PopoverTrigger>
                <PopoverContent
                  className='w-80 max-w-sm p-0'
                  align='start'
                  sideOffset={4}
                  collisionPadding={12}
                  onOpenAutoFocus={(e) => e.preventDefault()}
                >
                  <Command shouldFilter={true}>
                    <CommandInput placeholder='Search presets...' />
                    <CommandList
                      className='max-h-64 overflow-y-auto overscroll-contain'
                      onWheel={(e) => e.stopPropagation()}
                    >
                      {isLoadingPresets ? (
                        <CommandEmpty>Loading presets...</CommandEmpty>
                      ) : presets.length === 0 ? (
                        <CommandEmpty>No presets available.</CommandEmpty>
                      ) : (
                        <CommandGroup heading='Presets'>
                          {presets.map((preset) => (
                            <CommandItem
                              key={preset.id}
                              value={preset.id}
                              onSelect={() => {
                                applyModelToForm(preset);
                                setIsPresetOpen(false);
                              }}
                            >
                              <div className='flex flex-col text-sm'>
                                <span className='font-medium'>{preset.id}</span>
                                <span className='text-muted-foreground text-xs'>
                                  {preset.name}
                                </span>
                              </div>
                            </CommandItem>
                          ))}
                        </CommandGroup>
                      )}
                    </CommandList>
                  </Command>
                </PopoverContent>
              </Popover>
            </div>
            <div className='text-muted-foreground mt-1 text-xs'>
              Prefill fields from a preset model definition, then adjust as
              needed.
            </div>
          </div>
        )}
        <Form {...form}>
          <form onSubmit={form.handleSubmit(onSubmit)} className='space-y-4'>
            <div className='grid grid-cols-1 gap-4 sm:grid-cols-2'>
              <FormField
                control={form.control}
                name='id'
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Model ID *</FormLabel>
                    <FormControl>
                      <Input
                        placeholder='e.g., gpt-5.1'
                        {...field}
                        disabled={isOverride || isEdit}
                      />
                    </FormControl>
                    <FormDescription>
                      Unique identifier for the model
                    </FormDescription>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name='name'
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Display Name *</FormLabel>
                    <FormControl>
                      <Input placeholder='e.g., GPT-5.1' {...field} />
                    </FormControl>
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
                      placeholder='Brief description...'
                      {...field}
                      rows={2}
                    />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            <div className='grid grid-cols-1 gap-4 sm:grid-cols-2'>
              <FormField
                control={form.control}
                name='modality'
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Modality *</FormLabel>
                    <FormControl>
                      <Input
                        placeholder='text, text+image->text, image->text, etc.'
                        {...field}
                      />
                    </FormControl>
                    <FormDescription>
                      Composite modality label (e.g., text+image-&gt;text)
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
                    <FormLabel>Context Length</FormLabel>
                    <FormControl>
                      <Input type='number' {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>

            <div className='grid grid-cols-1 gap-4 sm:grid-cols-2'>
              <FormField
                control={form.control}
                name='input_modalities_raw'
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Input Modalities</FormLabel>
                    <FormControl>
                      <Input placeholder='text, image, file' {...field} />
                    </FormControl>
                    <FormDescription>Comma-separated list</FormDescription>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name='output_modalities_raw'
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Output Modalities</FormLabel>
                    <FormControl>
                      <Input placeholder='text, image' {...field} />
                    </FormControl>
                    <FormDescription>Comma-separated list</FormDescription>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>

            <div className='grid grid-cols-1 gap-4 sm:grid-cols-2'>
              <FormField
                control={form.control}
                name='tokenizer'
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Tokenizer</FormLabel>
                    <FormControl>
                      <Input placeholder='e.g., GPT, tiktoken' {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name='instruct_type'
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Instruct Type</FormLabel>
                    <FormControl>
                      <Input placeholder='e.g., chat, completion' {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>

            <div className='grid grid-cols-1 gap-4 sm:grid-cols-2'>
              <FormField
                control={form.control}
                name='canonical_slug'
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Canonical Slug</FormLabel>
                    <FormControl>
                      <Input
                        placeholder='google/gemini-3-pro-preview-20251117'
                        {...field}
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name='alias_ids_raw'
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Alias IDs</FormLabel>
                    <FormControl>
                      <Input placeholder='alias-1, alias-2' {...field} />
                    </FormControl>
                    <FormDescription>Comma-separated list</FormDescription>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>

            <div className='grid grid-cols-1 gap-4 sm:grid-cols-2'>
              <FormField
                control={form.control}
                name='upstream_provider_id'
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Upstream Provider ID</FormLabel>
                    <FormControl>
                      <Input placeholder='gemini' {...field} />
                    </FormControl>
                    <FormDescription>
                      Defaults to current provider if left blank
                    </FormDescription>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name='per_request_limits_raw'
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Per-request Limits (JSON)</FormLabel>
                    <FormControl>
                      <Textarea
                        placeholder='{"requests_per_min": 60}'
                        {...field}
                        rows={4}
                      />
                    </FormControl>
                    <FormDescription>
                      JSON object; leave empty for none
                    </FormDescription>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>

            <div className='bg-muted/20 space-y-4 rounded-md border p-4'>
              <h4 className='text-sm font-medium'>
                Pricing (USD per 1M tokens)
              </h4>
              <div className='grid grid-cols-1 gap-4 sm:grid-cols-2'>
                <FormField
                  control={form.control}
                  name='input_cost'
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Input Cost</FormLabel>
                      <FormControl>
                        <Input type='number' step='0.01' {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name='output_cost'
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Output Cost</FormLabel>
                      <FormControl>
                        <Input type='number' step='0.01' {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name='request_cost'
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Per Request Cost</FormLabel>
                      <FormControl>
                        <Input type='number' step='0.0001' {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name='image_cost'
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Image Cost (per image)</FormLabel>
                      <FormControl>
                        <Input type='number' step='0.0001' {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name='web_search_cost'
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Web Search Cost</FormLabel>
                      <FormControl>
                        <Input type='number' step='0.0001' {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name='internal_reasoning_cost'
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Internal Reasoning Cost</FormLabel>
                      <FormControl>
                        <Input type='number' step='0.0001' {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name='max_prompt_cost'
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Max Prompt Cost</FormLabel>
                      <FormControl>
                        <Input type='number' step='0.0001' {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name='max_completion_cost'
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Max Completion Cost</FormLabel>
                      <FormControl>
                        <Input type='number' step='0.0001' {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name='max_cost'
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Max Total Cost</FormLabel>
                      <FormControl>
                        <Input type='number' step='0.0001' {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
              </div>
            </div>

            <div className='bg-muted/20 space-y-4 rounded-md border p-4'>
              <h4 className='text-sm font-medium'>Top Provider (optional)</h4>
              <div className='grid grid-cols-1 gap-4 sm:grid-cols-3'>
                <FormField
                  control={form.control}
                  name='top_provider_context_length'
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Context Length</FormLabel>
                      <FormControl>
                        <Input
                          type='number'
                          {...field}
                          value={field.value ?? ''}
                        />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name='top_provider_max_completion_tokens'
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Max Completion Tokens</FormLabel>
                      <FormControl>
                        <Input
                          type='number'
                          {...field}
                          value={field.value ?? ''}
                        />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name='top_provider_is_moderated'
                  render={({ field }) => (
                    <FormItem className='flex flex-row items-center justify-between rounded-lg border p-4'>
                      <div className='space-y-0.5'>
                        <FormLabel className='text-base'>
                          Is Moderated
                        </FormLabel>
                        <FormDescription>
                          Whether provider enforces moderation
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
              </div>
            </div>

            <FormField
              control={form.control}
              name='enabled'
              render={({ field }) => (
                <FormItem className='flex flex-row items-center justify-between rounded-lg border p-4'>
                  <div className='space-y-0.5'>
                    <FormLabel className='text-base'>Enabled</FormLabel>
                    <FormDescription>Enable this model for use</FormDescription>
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

            <DialogFooter>
              <Button
                type='button'
                variant='outline'
                onClick={onClose}
                disabled={isSubmitting}
              >
                Cancel
              </Button>
              <Button type='submit' disabled={isSubmitting}>
                {isSubmitting && (
                  <Loader2 className='mr-2 h-4 w-4 animate-spin' />
                )}
                {isEdit
                  ? 'Save Changes'
                  : isOverride
                    ? 'Create Override'
                    : 'Create Model'}
              </Button>
            </DialogFooter>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  );
}
