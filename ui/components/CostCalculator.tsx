'use client';

import React, { useState, useMemo } from 'react';
import { type Model } from '@/lib/api/schemas/models';
import {
  calculateRequestCost,
  estimateMinimumTokensForCost,
  formatCost,
} from '@/lib/services/costValidation';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';

import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Info, AlertTriangle, CheckCircle } from 'lucide-react';

interface CostCalculatorProps {
  model: Model;
  testInput?: string;
  onTestInputChange?: (value: string) => void;
}

export function CostCalculator({ model }: CostCalculatorProps) {
  const [inputTokens, setInputTokens] = useState<number>(100);
  const [outputTokens, setOutputTokens] = useState<number>(100);

  // Calculate costs based on current input
  const costCalculation = useMemo(() => {
    return calculateRequestCost({
      inputTokens,
      outputTokens,
      model,
    });
  }, [inputTokens, outputTokens, model]);

  // Get minimum token estimates
  const tokenEstimates = useMemo(() => {
    return estimateMinimumTokensForCost(model);
  }, [model]);

  const hasMinimumCost = model.min_cost_per_request > 0;

  return (
    <div className='space-y-6'>
      {/* Input Controls */}
      <div className='grid grid-cols-1 gap-4 sm:grid-cols-2'>
        <div className='space-y-2'>
          <Label htmlFor='input-tokens'>Input Tokens</Label>
          <Input
            id='input-tokens'
            type='number'
            min='0'
            value={inputTokens}
            onChange={(e) => setInputTokens(parseInt(e.target.value) || 0)}
            placeholder='100'
          />
        </div>
        <div className='space-y-2'>
          <Label htmlFor='output-tokens'>Output Tokens</Label>
          <Input
            id='output-tokens'
            type='number'
            min='0'
            value={outputTokens}
            onChange={(e) => setOutputTokens(parseInt(e.target.value) || 0)}
            placeholder='100'
          />
        </div>
      </div>

      {/* Cost Breakdown */}
      <div className='rounded-md border p-4'>
        <h4 className='mb-3 text-sm font-medium'>Cost Breakdown</h4>
        <div className='space-y-2 text-sm'>
          <div className='flex justify-between'>
            <span>Input Cost ({inputTokens.toLocaleString()} tokens):</span>
            <span className='font-mono'>
              {formatCost(costCalculation.inputCost)}
            </span>
          </div>
          <div className='flex justify-between'>
            <span>Output Cost ({outputTokens.toLocaleString()} tokens):</span>
            <span className='font-mono'>
              {formatCost(costCalculation.outputCost)}
            </span>
          </div>
          <hr className='my-2' />
          <div className='flex justify-between'>
            <span>Base Cost:</span>
            <span className='font-mono'>
              {formatCost(costCalculation.baseCost)}
            </span>
          </div>
          <div className='flex justify-between'>
            <span>Minimum Cost per Request:</span>
            <span className='font-mono'>
              {formatCost(costCalculation.minCostPerRequest)}
            </span>
          </div>
          <hr className='my-2' />
          <div className='flex justify-between font-medium'>
            <span>Final Cost:</span>
            <span className='font-mono text-lg'>
              {formatCost(costCalculation.finalCost)}
            </span>
          </div>
        </div>
      </div>

      {/* Minimum Cost Alert */}
      {hasMinimumCost && (
        <Alert
          className={
            costCalculation.isMinimumApplied
              ? 'border-amber-200 bg-amber-50'
              : 'border-green-200 bg-green-50'
          }
        >
          {costCalculation.isMinimumApplied ? (
            <AlertTriangle className='h-4 w-4 text-amber-600' />
          ) : (
            <CheckCircle className='h-4 w-4 text-green-600' />
          )}
          <AlertTitle>
            {costCalculation.isMinimumApplied
              ? 'Minimum Cost Applied'
              : 'Above Minimum Cost'}
          </AlertTitle>
          <AlertDescription>
            {costCalculation.isMinimumApplied
              ? `The calculated cost (${formatCost(costCalculation.baseCost)}) is below the minimum, so the minimum cost of ${formatCost(costCalculation.minCostPerRequest)} is applied.`
              : `The calculated cost (${formatCost(costCalculation.baseCost)}) meets the minimum requirement of ${formatCost(costCalculation.minCostPerRequest)}.`}
          </AlertDescription>
        </Alert>
      )}

      {/* Token Recommendations */}
      {hasMinimumCost && tokenEstimates.inputTokensOnly > 0 && (
        <div className='rounded-md border p-4'>
          <h4 className='mb-3 flex items-center gap-2 text-sm font-medium'>
            <Info className='h-4 w-4' />
            Token Recommendations to Meet Minimum Cost
          </h4>
          <div className='space-y-2 text-sm'>
            <div className='flex justify-between'>
              <span>Input tokens only:</span>
              <span className='font-mono'>
                {tokenEstimates.inputTokensOnly.toLocaleString()}
              </span>
            </div>
            {tokenEstimates.outputTokensOnly > 0 && (
              <div className='flex justify-between'>
                <span>Output tokens only:</span>
                <span className='font-mono'>
                  {tokenEstimates.outputTokensOnly.toLocaleString()}
                </span>
              </div>
            )}
            <div className='flex justify-between'>
              <span>Balanced (50/50):</span>
              <span className='font-mono'>
                {tokenEstimates.balancedTokens.input.toLocaleString()} in +{' '}
                {tokenEstimates.balancedTokens.output.toLocaleString()} out
              </span>
            </div>
          </div>
          <div className='mt-3 flex gap-2'>
            <Button
              variant='outline'
              size='sm'
              onClick={() => {
                setInputTokens(tokenEstimates.inputTokensOnly);
                setOutputTokens(0);
              }}
            >
              Use Input Only
            </Button>
            {tokenEstimates.outputTokensOnly > 0 && (
              <Button
                variant='outline'
                size='sm'
                onClick={() => {
                  setInputTokens(0);
                  setOutputTokens(tokenEstimates.outputTokensOnly);
                }}
              >
                Use Output Only
              </Button>
            )}
            <Button
              variant='outline'
              size='sm'
              onClick={() => {
                setInputTokens(tokenEstimates.balancedTokens.input);
                setOutputTokens(tokenEstimates.balancedTokens.output);
              }}
            >
              Use Balanced
            </Button>
          </div>
        </div>
      )}

      {/* Model Pricing Info */}
      <div className='rounded-md border p-4'>
        <h4 className='mb-3 text-sm font-medium'>Model Pricing</h4>
        <div className='space-y-2 text-sm'>
          <div className='flex justify-between'>
            <span>Input cost per 1M tokens:</span>
            <span className='font-mono'>{formatCost(model.input_cost)}</span>
          </div>
          <div className='flex justify-between'>
            <span>Output cost per 1M tokens:</span>
            <span className='font-mono'>{formatCost(model.output_cost)}</span>
          </div>
          <div className='flex justify-between'>
            <span>Minimum cost per request:</span>
            <span className='font-mono'>
              {formatCost(model.min_cost_per_request)}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
