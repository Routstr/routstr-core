import {
  calculateRequestCost,
  validateRequestCost,
  estimateMinimumTokensForCost,
  formatCost,
  formatCostBreakdown,
} from '../costValidation';
import { type Model } from '../../api/schemas/models';

// Mock model for testing
const mockModel: Model = {
  id: 'test-model',
  name: 'Test Model',
  modelType: 'text',
  isEnabled: true,
  createdAt: new Date().toISOString(),
  updatedAt: new Date().toISOString(),
  provider: 'Test Provider',
  url: 'https://api.test.com',
  input_cost: 1.0, // $1.00 per 1M tokens
  output_cost: 2.0, // $2.00 per 1M tokens
  min_cost_per_request: 0.005, // $0.005 minimum per request
  apiKeyRequired: true,
};

const mockModelNoMinimum: Model = {
  ...mockModel,
  id: 'test-model-no-min',
  min_cost_per_request: 0,
};

describe('calculateRequestCost', () => {
  it('should calculate costs correctly when above minimum', () => {
    const result = calculateRequestCost({
      inputTokens: 1000000, // 1M tokens = $1.00
      outputTokens: 2000000, // 2M tokens = $4.00
      model: mockModel,
    });

    expect(result.inputCost).toBe(1.0);
    expect(result.outputCost).toBe(4.0);
    expect(result.baseCost).toBe(5.0);
    expect(result.minCostPerRequest).toBe(0.005);
    expect(result.finalCost).toBe(5.0);
    expect(result.isMinimumApplied).toBe(false);
  });

  it('should apply minimum cost when base cost is below minimum', () => {
    const result = calculateRequestCost({
      inputTokens: 1000, // 1K tokens = $0.001
      outputTokens: 1000, // 1K tokens = $0.002
      model: mockModel,
    });

    expect(result.inputCost).toBe(0.001);
    expect(result.outputCost).toBe(0.002);
    expect(result.baseCost).toBe(0.003);
    expect(result.minCostPerRequest).toBe(0.005);
    expect(result.finalCost).toBe(0.005);
    expect(result.isMinimumApplied).toBe(true);
  });

  it('should work with models that have no minimum cost', () => {
    const result = calculateRequestCost({
      inputTokens: 1000,
      outputTokens: 1000,
      model: mockModelNoMinimum,
    });

    expect(result.baseCost).toBe(0.003);
    expect(result.minCostPerRequest).toBe(0);
    expect(result.finalCost).toBe(0.003);
    expect(result.isMinimumApplied).toBe(false);
  });

  it('should handle zero token usage', () => {
    const result = calculateRequestCost({
      inputTokens: 0,
      outputTokens: 0,
      model: mockModel,
    });

    expect(result.baseCost).toBe(0);
    expect(result.finalCost).toBe(0.005);
    expect(result.isMinimumApplied).toBe(true);
  });
});

describe('validateRequestCost', () => {
  it('should validate successful requests above minimum', () => {
    const validation = validateRequestCost(mockModel, 1000000, 2000000);

    expect(validation.isValid).toBe(true);
    expect(validation.actualCost).toBe(5.0);
    expect(validation.minimumCost).toBe(0.005);
    expect(validation.message).toBeUndefined();
  });

  it('should validate requests where minimum is applied', () => {
    const validation = validateRequestCost(mockModel, 1000, 1000);

    expect(validation.isValid).toBe(true);
    expect(validation.actualCost).toBe(0.005);
    expect(validation.minimumCost).toBe(0.005);
    expect(validation.message).toContain('Minimum cost of $0.005000 applied');
  });

  it('should validate requests with no minimum cost', () => {
    const validation = validateRequestCost(mockModelNoMinimum, 1000, 1000);

    expect(validation.isValid).toBe(true);
    expect(validation.actualCost).toBe(0.003);
    expect(validation.minimumCost).toBe(0);
  });
});

describe('estimateMinimumTokensForCost', () => {
  it('should calculate token estimates correctly', () => {
    const estimates = estimateMinimumTokensForCost(mockModel);

    expect(estimates.inputTokensOnly).toBe(5000); // 0.005 / 1.0 * 1M
    expect(estimates.outputTokensOnly).toBe(2500); // 0.005 / 2.0 * 1M
    expect(estimates.balancedTokens.input).toBe(2500); // Half cost via input
    expect(estimates.balancedTokens.output).toBe(1250); // Half cost via output
  });

  it('should return zeros for models with no minimum cost', () => {
    const estimates = estimateMinimumTokensForCost(mockModelNoMinimum);

    expect(estimates.inputTokensOnly).toBe(0);
    expect(estimates.outputTokensOnly).toBe(0);
    expect(estimates.balancedTokens.input).toBe(0);
    expect(estimates.balancedTokens.output).toBe(0);
  });

  it('should handle models with zero costs gracefully', () => {
    const freeModel: Model = {
      ...mockModel,
      input_cost: 0,
      output_cost: 0,
      min_cost_per_request: 0.001,
    };

    const estimates = estimateMinimumTokensForCost(freeModel);

    expect(estimates.inputTokensOnly).toBe(0);
    expect(estimates.outputTokensOnly).toBe(0);
    expect(estimates.balancedTokens.input).toBe(0);
    expect(estimates.balancedTokens.output).toBe(0);
  });
});

describe('formatCost', () => {
  it('should format costs correctly', () => {
    expect(formatCost(0)).toBe('Free');
    expect(formatCost(0.000001)).toBe('$0.00000100');
    expect(formatCost(0.0001)).toBe('$0.000100');
    expect(formatCost(0.001)).toBe('$0.0010');
    expect(formatCost(0.01)).toBe('$0.010');
    expect(formatCost(0.1)).toBe('$0.100');
    expect(formatCost(1.0)).toBe('$1.000');
  });
});

describe('formatCostBreakdown', () => {
  it('should format cost breakdown with minimum applied', () => {
    const calculation = calculateRequestCost({
      inputTokens: 100,
      outputTokens: 100,
      model: mockModel,
    });

    const breakdown = formatCostBreakdown(calculation);
    expect(breakdown).toContain('Input: $0.000100');
    expect(breakdown).toContain('Output: $0.000200');
    expect(breakdown).toContain('Minimum: $0.005000');
  });

  it('should format cost breakdown without minimum', () => {
    const calculation = calculateRequestCost({
      inputTokens: 1000,
      outputTokens: 2000,
      model: mockModel,
    });

    const breakdown = formatCostBreakdown(calculation);
    expect(breakdown).toContain('Input: $0.0010');
    expect(breakdown).toContain('Output: $0.0040');
    expect(breakdown).not.toContain('Minimum:');
  });

  it('should handle zero costs', () => {
    const calculation = calculateRequestCost({
      inputTokens: 0,
      outputTokens: 100,
      model: mockModel,
    });

    const breakdown = formatCostBreakdown(calculation);
    expect(breakdown).not.toContain('Input:');
    expect(breakdown).toContain('Output: $0.000200');
    expect(breakdown).toContain('Minimum: $0.005000');
  });
});

describe('Edge Cases', () => {
  it('should handle very large token counts', () => {
    const result = calculateRequestCost({
      inputTokens: 100000000, // 100M tokens
      outputTokens: 100000000, // 100M tokens
      model: mockModel,
    });

    expect(result.inputCost).toBe(100.0);
    expect(result.outputCost).toBe(200.0);
    expect(result.finalCost).toBe(300.0);
    expect(result.isMinimumApplied).toBe(false);
  });

  it('should handle fractional token counts', () => {
    const result = calculateRequestCost({
      inputTokens: 500000, // 0.5M tokens
      outputTokens: 750000, // 0.75M tokens
      model: mockModel,
    });

    expect(result.inputCost).toBe(0.5);
    expect(result.outputCost).toBe(1.5);
    expect(result.finalCost).toBe(2.0);
    expect(result.isMinimumApplied).toBe(false);
  });

  it('should handle models with very high minimum costs', () => {
    const expensiveModel: Model = {
      ...mockModel,
      min_cost_per_request: 10.0,
    };

    const result = calculateRequestCost({
      inputTokens: 1000000, // 1M tokens
      outputTokens: 1000000, // 1M tokens
      model: expensiveModel,
    });

    expect(result.finalCost).toBe(10.0);
    expect(result.isMinimumApplied).toBe(true);
  });
});
