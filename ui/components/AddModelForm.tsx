'use client';

import React, { useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { ManualModelSchema, type ManualModel } from '@/lib/api/schemas/models';
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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Form,
  FormControl,
  FormDescription,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from '@/components/ui/form';
import { Plus, Loader2 } from 'lucide-react';
import { toast } from 'sonner';

interface AddModelFormProps {
  onModelAdd: (model: ManualModel) => void;
  onCancel?: () => void;
  isOpen: boolean;
}

export function AddModelForm({
  onModelAdd,
  onCancel,
  isOpen,
}: AddModelFormProps) {
  const [isSubmitting, setIsSubmitting] = useState(false);

  const form = useForm<ManualModel>({
    resolver: zodResolver(ManualModelSchema) as any, // eslint-disable-line @typescript-eslint/no-explicit-any
    defaultValues: {
      name: '',
      full_name: '',
      input_cost: 0,
      output_cost: 0,
      provider: '',
      modelType: 'text' as const,
      description: '',
      contextLength: undefined,
    },
  });

  const onSubmit = async (data: ManualModel) => {
    setIsSubmitting(true);
    try {
      await onModelAdd(data);
      toast.success('Model added successfully!');
      form.reset();
      onCancel?.();
    } catch (error) {
      toast.error('Failed to add model. Please try again.');
      console.error('Error adding model:', error);
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleClose = () => {
    if (!isSubmitting) {
      form.reset();
      onCancel?.();
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={handleClose}>
      <DialogContent className='max-h-[90vh] overflow-y-auto sm:max-w-[600px]'>
        <DialogHeader>
          <DialogTitle className='flex items-center gap-2'>
            <Plus className='h-5 w-5' />
            Add New Model
          </DialogTitle>
          <DialogDescription>
            Manually add a new AI model to your collection
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
                    <FormLabel>Model Name *</FormLabel>
                    <FormControl>
                      <Input
                        placeholder='e.g., GPT-4o'
                        {...field}
                        className='w-full'
                      />
                    </FormControl>
                    <FormDescription>
                      Display name for the model
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

            <div className='grid grid-cols-1 gap-4 sm:grid-cols-2'>
              <FormField
                control={form.control}
                name='modelType'
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Model Type</FormLabel>
                    <Select
                      onValueChange={field.onChange}
                      defaultValue={field.value}
                    >
                      <FormControl>
                        <SelectTrigger>
                          <SelectValue placeholder='Select model type' />
                        </SelectTrigger>
                      </FormControl>
                      <SelectContent>
                        <SelectItem value='text'>Text/Chat</SelectItem>
                        <SelectItem value='embedding'>Embedding</SelectItem>
                        <SelectItem value='image'>Image Generation</SelectItem>
                        <SelectItem value='audio'>Audio</SelectItem>
                        <SelectItem value='multimodal'>Multimodal</SelectItem>
                      </SelectContent>
                    </Select>
                    <FormDescription>
                      Type of AI model functionality
                    </FormDescription>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name='contextLength'
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Context Length</FormLabel>
                    <FormControl>
                      <Input
                        type='number'
                        min='0'
                        placeholder='8192'
                        value={field.value || ''}
                        onChange={(e) => {
                          const val = parseInt(e.target.value);
                          field.onChange(
                            isNaN(val) || val === 0 ? undefined : val
                          );
                        }}
                        className='w-full'
                      />
                    </FormControl>
                    <FormDescription>
                      Maximum context length in tokens (optional, leave empty
                      for default)
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
                    Adding...
                  </>
                ) : (
                  'Add Model'
                )}
              </Button>
            </div>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  );
}
