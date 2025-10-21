'use client';

import React, { useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import {
  GroupSettingsSchema,
  type GroupSettings,
  type Model,
} from '@/lib/api/schemas/models';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
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
import { Switch } from '@/components/ui/switch';
import { Users, Key, Loader2, Globe, AlertTriangle, Info } from 'lucide-react';
import { toast } from 'sonner';
import { Alert, AlertDescription } from '@/components/ui/alert';

interface EditGroupFormProps {
  provider: string;
  models: Model[];
  groupSettings?: { group_api_key?: string; group_url?: string };
  onGroupUpdate: (oldProvider: string, updatedData: GroupSettings) => void;
  onCancel?: () => void;
  isOpen: boolean;
}

export function EditGroupForm({
  provider,
  models,
  groupSettings,
  onGroupUpdate,
  onCancel,
  isOpen,
}: EditGroupFormProps) {
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [showApiKey, setShowApiKey] = useState(false);
  const [useGroupUrl, setUseGroupUrl] = useState(!!groupSettings?.group_url);

  const form = useForm<GroupSettings>({
    resolver: zodResolver(GroupSettingsSchema),
    defaultValues: {
      provider: provider,
      group_api_key: groupSettings?.group_api_key || '',
      group_url: groupSettings?.group_url || '',
    },
  });

  const onSubmit = async (data: GroupSettings) => {
    setIsSubmitting(true);
    try {
      // Clean up empty strings and handle URL removal
      const cleanData = {
        ...data,
        group_api_key: data.group_api_key?.trim() || undefined,
        group_url: useGroupUrl
          ? data.group_url?.trim() || undefined
          : undefined,
      };
      await onGroupUpdate(provider, cleanData);
      toast.success(`Group "${provider}" updated successfully!`);
      onCancel?.();
    } catch (error) {
      toast.error('Failed to update group. Please try again.');
      console.error('Error updating group:', error);
    } finally {
      setIsSubmitting(false);
    }
  };

  // Count models that have individual API keys
  const modelsWithIndividualKeys = models.filter(
    (model) => model.api_key
  ).length;
  const modelsWithoutKeys = models.filter((model) => !model.api_key).length;

  // Count models that would be affected by URL changes
  const modelsUsingGroupUrl = models.filter(
    (model) => !model.api_key && (!model.url || model.url.startsWith('/'))
  ).length;

  const handleClose = () => {
    if (!isSubmitting) {
      onCancel?.();
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={handleClose}>
      <DialogContent className='max-h-[90vh] overflow-y-auto sm:max-w-[700px]'>
        <DialogHeader>
          <DialogTitle className='flex items-center gap-2'>
            <Users className='h-5 w-5' />
            Edit Provider Group: {provider}
          </DialogTitle>
          <DialogDescription>
            Update settings for all {models.length} models in this group. Group
            settings provide defaults for models without individual
            configurations.
          </DialogDescription>
        </DialogHeader>

        <div className='mb-6 space-y-4'>
          {/* API Key Status */}
          <div className='rounded-md border p-4'>
            <h4 className='mb-2 flex items-center gap-2 text-sm font-medium'>
              <Key className='h-4 w-4' />
              API Key Configuration
            </h4>
            <div className='space-y-2 text-sm'>
              <div className='flex justify-between'>
                <span>Models using group API key:</span>
                <span className='font-medium text-blue-600'>
                  {modelsWithoutKeys}
                </span>
              </div>
            </div>
          </div>

          {/* URL Configuration Status */}
          <div className='rounded-md border p-4'>
            <h4 className='mb-2 flex items-center gap-2 text-sm font-medium'>
              <Globe className='h-4 w-4' />
              URL Configuration
            </h4>
            <div className='space-y-2 text-sm'>
              <div className='flex justify-between'>
                <span>Models using group URL:</span>
                <span className='font-medium text-blue-600'>
                  {modelsUsingGroupUrl}
                </span>
              </div>
              <div className='flex justify-between'>
                <span>Models with individual URLs:</span>
                <span className='font-medium text-green-600'>
                  {models.length - modelsUsingGroupUrl}
                </span>
              </div>
              {groupSettings?.group_url && (
                <div className='mt-2 rounded bg-blue-50 p-2 text-xs'>
                  <span className='font-medium'>Current group URL:</span>{' '}
                  {groupSettings.group_url}
                </div>
              )}
            </div>
          </div>

          {/* Models in this group */}
          <div className='rounded-md border p-4'>
            <h4 className='mb-2 text-sm font-medium'>Models in this group:</h4>
            <div className='max-h-32 overflow-y-auto'>
              <div className='flex flex-wrap gap-2'>
                {models.map((model) => (
                  <span
                    key={model.id}
                    className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${
                      model.api_key
                        ? 'bg-green-100 text-green-800'
                        : 'bg-blue-100 text-blue-800'
                    }`}
                    title={
                      model.api_key
                        ? 'Has individual API key and URL'
                        : 'Uses group API key and URL'
                    }
                  >
                    {model.name}
                    {model.api_key && ' 🔑'}
                    {model.url &&
                      !model.url.startsWith('/') &&
                      model.api_key &&
                      ' 🌐'}
                  </span>
                ))}
              </div>
            </div>
          </div>
        </div>

        <Form {...form}>
          <form onSubmit={form.handleSubmit(onSubmit)} className='space-y-6'>
            <FormField
              control={form.control}
              name='provider'
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Provider Name *</FormLabel>
                  <FormControl>
                    <Input
                      placeholder='e.g., OpenAI, Anthropic'
                      {...field}
                      className='w-full'
                    />
                  </FormControl>
                  <FormDescription>
                    This will update the provider name for all models in this
                    group
                  </FormDescription>
                  <FormMessage />
                </FormItem>
              )}
            />

            {/* Group URL Toggle */}
            <div className='space-y-4'>
              <div className='flex items-center justify-between'>
                <div className='space-y-0.5'>
                  <FormLabel className='text-base'>Group Base URL</FormLabel>
                  <FormDescription>
                    Provide a custom base URL for this provider group
                  </FormDescription>
                </div>
                <Switch
                  checked={useGroupUrl}
                  onCheckedChange={setUseGroupUrl}
                />
              </div>

              {useGroupUrl && (
                <FormField
                  control={form.control}
                  name='group_url'
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Base URL</FormLabel>
                      <FormControl>
                        <Input
                          placeholder='https://api.openai.com/v1 or https://your-proxy.com/v1'
                          {...field}
                          className='w-full'
                        />
                      </FormControl>
                      <FormDescription>
                        This base URL will be used for models without individual
                        URLs. Models will append their endpoint path (e.g.,
                        /chat/completions) to this base.
                      </FormDescription>
                      <FormMessage />
                    </FormItem>
                  )}
                />
              )}

              {!useGroupUrl && groupSettings?.group_url && (
                <Alert>
                  <AlertTriangle className='h-4 w-4' />
                  <AlertDescription>
                    Removing the group URL will make models in this group fall
                    back to the default system endpoint. Models with individual
                    URLs will be unaffected.
                  </AlertDescription>
                </Alert>
              )}

              {useGroupUrl && !groupSettings?.group_url && (
                <Alert>
                  <Info className='h-4 w-4' />
                  <AlertDescription>
                    Adding a group URL will allow models in this group to use a
                    custom endpoint instead of the default system endpoint.
                  </AlertDescription>
                </Alert>
              )}
            </div>

            <FormField
              control={form.control}
              name='group_api_key'
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Group API Key</FormLabel>
                  <div className='relative'>
                    <FormControl>
                      <Input
                        type={showApiKey ? 'text' : 'password'}
                        placeholder='sk-... (leave empty to remove)'
                        {...field}
                        className='w-full pr-24'
                      />
                    </FormControl>
                    <Button
                      type='button'
                      variant='ghost'
                      size='sm'
                      className='absolute top-0 right-0 h-full px-3 py-2 hover:bg-transparent'
                      onClick={() => setShowApiKey(!showApiKey)}
                    >
                      {showApiKey ? 'Hide' : 'Show'}
                    </Button>
                  </div>
                  <FormDescription>
                    This API key will be used for models that don&apos;t have
                    individual API keys ({modelsWithoutKeys} models). Leave
                    empty to remove the group API key.
                  </FormDescription>
                  <FormMessage />
                </FormItem>
              )}
            />

            <div className='rounded-md border border-amber-200 bg-amber-50 p-4'>
              <p className='text-sm text-amber-800'>
                <strong>How group settings work:</strong>
              </p>
              <ul className='mt-2 list-inside list-disc space-y-1 text-sm text-amber-800'>
                <li>
                  Models with individual API keys and URLs will keep their
                  specific settings
                </li>
                <li>
                  Models without individual settings will use the group defaults
                </li>
                <li>
                  Removing the group URL makes models fall back to the system
                  default endpoint
                </li>
                <li>
                  You can use &quot;Apply Group Settings&quot; to force models
                  to use group configurations
                </li>
              </ul>
            </div>

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
                    Updating...
                  </>
                ) : (
                  'Update Group'
                )}
              </Button>
            </div>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  );
}
