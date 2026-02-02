'use client';

import React, { useState } from 'react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Textarea } from '@/components/ui/textarea';
import { toast } from 'sonner';
import { AdminService } from '@/lib/api/services/admin';
import { Loader2, Database } from 'lucide-react';

export interface BatchOverrideDialogProps {
  providerId: number;
  isOpen: boolean;
  onClose: () => void;
  onSuccess: () => void;
}

export function BatchOverrideDialog({
  providerId,
  isOpen,
  onClose,
  onSuccess,
}: BatchOverrideDialogProps) {
  const [jsonInput, setJsonInput] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);

  const sampleJson = {
    models: [
      {
        id: 'model-id-1',
        name: 'Model Name 1',
        description: 'Description...',
        created: Math.floor(Date.now() / 1000),
        context_length: 8192,
        architecture: {
          modality: 'text',
          input_modalities: ['text'],
          output_modalities: ['text'],
          tokenizer: '',
          instruct_type: null,
        },
        pricing: {
          prompt: 0.0,
          completion: 0.0,
          request: 0.0,
          image: 0.0,
          web_search: 0.0,
          internal_reasoning: 0.0,
        },
        enabled: true,
      },
    ],
  };

  const handleBatchOverride = async () => {
    if (!jsonInput.trim()) {
      toast.error('Please enter JSON content');
      return;
    }

    setIsSubmitting(true);
    try {
      let data;
      try {
        data = JSON.parse(jsonInput);
      } catch {
        throw new Error('Invalid JSON format');
      }

      if (!data.models || !Array.isArray(data.models)) {
        throw new Error('JSON match follow structure: { "models": [...] }');
      }

      const result = await AdminService.batchOverrideProviderModels(
        providerId,
        data.models
      );

      if (result.ok) {
        toast.success(result.message || 'Batch override successful');
        onSuccess();
        onClose();
        setJsonInput('');
      } else {
        throw new Error('Batch override failed');
      }
    } catch (error: unknown) {
      const message =
        error instanceof Error ? error.message : 'Batch override failed';
      toast.error(message);
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className='sm:max-w-[800px]'>
        <DialogHeader>
          <DialogTitle className='flex items-center gap-2'>
            <Database className='h-4 w-4' />
            Batch Override Models
          </DialogTitle>
          <DialogDescription>
            Paste a JSON object with a &quot;models&quot; array containing model
            definitions. Existing models with the same ID will be updated.
          </DialogDescription>
        </DialogHeader>

        <div className='grid gap-4 py-4'>
          <Textarea
            value={jsonInput}
            onChange={(e) => setJsonInput(e.target.value)}
            placeholder={JSON.stringify(sampleJson, null, 2)}
            className='min-h-[400px] font-mono text-xs'
          />
        </div>

        <DialogFooter>
          <Button variant='outline' onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={handleBatchOverride} disabled={isSubmitting}>
            {isSubmitting ? (
              <>
                <Loader2 className='mr-2 h-4 w-4 animate-spin' />
                Processing...
              </>
            ) : (
              'Batch Override'
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
