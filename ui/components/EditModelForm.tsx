'use client';

import React, { useState, useEffect } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import {
  ManualModelSchema,
  type ManualModel,
  type Model,
} from '@/lib/api/schemas/models';
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

interface EditModelFormProps {
  model: Model;
  onModelUpdate: (modelId: string, updatedModel: ManualModel) => void;
  onCancel?: () => void;
  isOpen: boolean;
}

export function EditModelForm({
  model,
  onModelUpdate,
  onCancel,
  isOpen,
}: EditModelFormProps) {
  const [isSubmitting, setIsSubmitting] = useState(false);

  const form = useForm<ManualModel>({
    resolver: zodResolver(ManualModelSchema) as any, // eslint-disable-line @typescript-eslint/no-explicit-any
    defaultValues: {
      name: model.name,
      full_name: model.full_name,
      input_cost: model.input_cost,
      output_cost: model.output_cost,
      provider: model.provider,
      modelType: model.modelType as ManualModel['modelType'],
      description: model.description || '',
      contextLength: model.contextLength || 0,
    },
  });

  // Reset form when model changes
  useEffect(() => {
    form.reset({
      name: model.name,
      full_name: model.full_name,
      input_cost: model.input_cost,
      output_cost: model.output_cost,
      provider: model.provider,
      modelType: model.modelType as ManualModel['modelType'],
      description: model.description || '',
      contextLength: model.contextLength || 0,
    });
  }, [model, form]);

  const onSubmit = async (data: ManualModel) => {
    setIsSubmitting(true);
    try {
      // Ensure we're sending the correct API key value
      const updatedData = {
        ...data,
      };
      await onModelUpdate(model.id, updatedData);
      toast.success('Model updated successfully!');
      onCancel?.();
    } catch (error) {
      toast.error('Failed to update model. Please try again.');
      console.error('Error updating model:', error);
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
            Edit Model
          </DialogTitle>
          <DialogDescription>
            Update the details for &quot;{model.name}&quot;
          </DialogDescription>
        </DialogHeader>

        <Form {...form}>
          <form onSubmit={form.handleSubmit(onSubmit)} className='space-y-4'>
            <div className='grid grid-cols-1 gap-4 sm:grid-cols-2'>
              <FormField
                control={form.control}
                name='full_name'
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Original Model Name</FormLabel>
                    <FormControl>
                      <Input {...field} className='bg-muted w-full' disabled />
                    </FormControl>
                    <FormDescription>
                      Original name from the provider (cannot be changed)
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
                name='provider'
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Provider *</FormLabel>
                    <FormControl>
                      <Input
                        placeholder='e.g., OpenAI, Anthropic'
                        {...field}
                        className='w-full'
                      />
                    </FormControl>
                    <FormDescription>
                      AI model provider or company
                    </FormDescription>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>

            <div className='grid grid-cols-1 gap-4 sm:grid-cols-2'>
              <FormField
                control={form.control}
                name='input_cost'
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Input Cost (per 1M tokens) *</FormLabel>
                    <FormControl>
                      <Input
                        type='number'
                        step='0.001'
                        min='0'
                        placeholder='5.00'
                        {...field}
                        value={
                          field.value ? parseFloat(field.value.toFixed(3)) : ''
                        }
                        onChange={(e) => {
                          const value = parseFloat(e.target.value) || 0;
                          const rounded = Math.round(value * 1000) / 1000;
                          field.onChange(rounded);
                        }}
                        className='w-full'
                      />
                    </FormControl>
                    <FormDescription>
                      Cost in USD per 1,000,000 input tokens (max 3 decimals)
                    </FormDescription>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name='output_cost'
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Output Cost (per 1M tokens) *</FormLabel>
                    <FormControl>
                      <Input
                        type='number'
                        step='0.001'
                        min='0'
                        placeholder='15.00'
                        {...field}
                        value={
                          field.value ? parseFloat(field.value.toFixed(3)) : ''
                        }
                        onChange={(e) => {
                          const value = parseFloat(e.target.value) || 0;
                          const rounded = Math.round(value * 1000) / 1000;
                          field.onChange(rounded);
                        }}
                        className='w-full'
                      />
                    </FormControl>
                    <FormDescription>
                      Cost in USD per 1,000,000 output tokens (max 3 decimals)
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
