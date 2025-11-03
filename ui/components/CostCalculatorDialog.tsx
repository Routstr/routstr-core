'use client';

import React from 'react';
import { type Model } from '@/lib/api/schemas/models';
import { CostCalculator } from '@/components/CostCalculator';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Calculator } from 'lucide-react';

interface CostCalculatorDialogProps {
  model: Model;
  isOpen: boolean;
  onClose: () => void;
}

export function CostCalculatorDialog({
  model,
  isOpen,
  onClose,
}: CostCalculatorDialogProps) {
  return (
    <Dialog open={isOpen} onOpenChange={onClose}>
      <DialogContent className='max-h-[90vh] overflow-y-auto sm:max-w-[700px]'>
        <DialogHeader>
          <DialogTitle className='flex items-center gap-2'>
            <Calculator className='h-5 w-5' />
            Cost Calculator - {model.name}
          </DialogTitle>
          <DialogDescription>
            Calculate costs and estimate token usage for &quot;{model.name}
            &quot;
          </DialogDescription>
        </DialogHeader>

        <div className='mt-4'>
          <CostCalculator model={model} />
        </div>
      </DialogContent>
    </Dialog>
  );
}
