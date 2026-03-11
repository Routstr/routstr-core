import { type Model } from '@/lib/api/schemas/models';

export interface CostCalculationInput {
  inputTokens: number;
  outputTokens: number;
  model: Model;
}

export interface CostCalculationResult {
  inputCost: number;
  outputCost: number;
  baseCost: number; // Sum of input and output costs
  minCostPerRequest: number;
  finalCost: number; // Max of baseCost and minCostPerRequest
  isMinimumApplied: boolean; // Whether minimum cost was applied
}

export interface RequestCostValidation {
  isValid: boolean;
  actualCost: number;
  minimumCost: number;
  message?: string;
}

/**
 * Calculate the cost for a model request
 */
export function calculateRequestCost(
  input: CostCalculationInput
): CostCalculationResult {
  const { inputTokens, outputTokens, model } = input;

  // Calculate base costs based on token usage (per 1M tokens)
  const inputCost = (inputTokens / 1000000) * model.input_cost;
  const outputCost = (outputTokens / 1000000) * model.output_cost;
  const baseCost = inputCost + outputCost;

  // Apply minimum cost per request
  const minCostPerRequest = model.min_cost_per_request || 0;
  const finalCost = Math.max(baseCost, minCostPerRequest);
  const isMinimumApplied = finalCost > baseCost;

  return {
    inputCost,
    outputCost,
    baseCost,
    minCostPerRequest,
    finalCost,
    isMinimumApplied,
  };
}

/**
 * Validate that a request meets the minimum cost requirements
 */
export function validateRequestCost(
  model: Model,
  inputTokens: number,
  outputTokens: number
): RequestCostValidation {
  const calculation = calculateRequestCost({
    inputTokens,
    outputTokens,
    model,
  });

  const isValid = calculation.finalCost >= calculation.minCostPerRequest;

  if (!isValid) {
    return {
      isValid: false,
      actualCost: calculation.baseCost,
      minimumCost: calculation.minCostPerRequest,
      message: `Request cost ($${calculation.baseCost.toFixed(6)}) is below the minimum required cost ($${calculation.minCostPerRequest.toFixed(6)}) for model "${model.name}".`,
    };
  }

  return {
    isValid: true,
    actualCost: calculation.finalCost,
    minimumCost: calculation.minCostPerRequest,
    message: calculation.isMinimumApplied
      ? `Minimum cost of $${calculation.minCostPerRequest.toFixed(6)} applied.`
      : undefined,
  };
}

/**
 * Estimate the minimum tokens needed to meet the minimum cost requirement
 */
export function estimateMinimumTokensForCost(model: Model): {
  inputTokensOnly: number;
  outputTokensOnly: number;
  balancedTokens: { input: number; output: number };
} {
  const minCost = model.min_cost_per_request || 0;

  if (minCost === 0) {
    return {
      inputTokensOnly: 0,
      outputTokensOnly: 0,
      balancedTokens: { input: 0, output: 0 },
    };
  }

  // Calculate tokens needed if using only input tokens (per 1M tokens)
  const inputTokensOnly =
    model.input_cost > 0
      ? Math.ceil((minCost * 1000000) / model.input_cost)
      : 0;

  // Calculate tokens needed if using only output tokens (per 1M tokens)
  const outputTokensOnly =
    model.output_cost > 0
      ? Math.ceil((minCost * 1000000) / model.output_cost)
      : 0;

  // Calculate balanced approach (50/50 split)
  const halfCost = minCost / 2;
  const balancedInput =
    model.input_cost > 0
      ? Math.ceil((halfCost * 1000000) / model.input_cost)
      : 0;
  const balancedOutput =
    model.output_cost > 0
      ? Math.ceil((halfCost * 1000000) / model.output_cost)
      : 0;

  return {
    inputTokensOnly,
    outputTokensOnly,
    balancedTokens: { input: balancedInput, output: balancedOutput },
  };
}

/**
 * Format cost for display
 */
export function formatCost(cost: number): string {
  if (cost === 0) return 'Free';
  if (cost < 0.000001) return `$${cost.toFixed(8)}`;
  if (cost < 0.001) return `$${cost.toFixed(6)}`;
  if (cost < 0.01) return `$${cost.toFixed(4)}`;
  return `$${cost.toFixed(3)}`;
}

/**
 * Format cost breakdown for display
 */
export function formatCostBreakdown(
  calculation: CostCalculationResult
): string {
  const parts = [];

  if (calculation.inputCost > 0) {
    parts.push(`Input: ${formatCost(calculation.inputCost)}`);
  }

  if (calculation.outputCost > 0) {
    parts.push(`Output: ${formatCost(calculation.outputCost)}`);
  }

  if (calculation.isMinimumApplied) {
    parts.push(`Minimum: ${formatCost(calculation.minCostPerRequest)}`);
  }

  return parts.join(' | ');
}

/**
 * Calculate minimum cost per request based on context length and input cost
 * Formula: (context_length / 1,000,000) * input_cost
 * This represents the cost if the full context window is used for input
 */
export function calculateMinCostPerRequest(
  contextLength: number | undefined,
  inputCost: number
): number {
  // If no context length is specified, use a reasonable default minimum
  if (!contextLength || contextLength <= 0) {
    // Use a minimum of 1000 tokens worth of input cost as base minimum
    return (1000 / 1_000_000) * inputCost;
  }

  // Calculate based on full context window usage
  const costForFullContext = (contextLength / 1_000_000) * inputCost;

  // Ensure there's always a reasonable minimum cost (at least 100 tokens worth)
  const minimumFloor = (100 / 1_000_000) * inputCost;

  return Math.max(costForFullContext, minimumFloor);
}
