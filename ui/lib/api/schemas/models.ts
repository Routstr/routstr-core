import { z } from 'zod';

// Base model schema that defines common properties for all models
export const ModelSchema = z.object({
  id: z.string(),
  name: z.string(),
  full_name: z.string(),
  description: z.string().optional(),
  modelType: z.string(), // (['text', 'embedding', 'image', 'audio', 'multimodal']),
  isEnabled: z.boolean(),
  createdAt: z.string().datetime(),
  updatedAt: z.string().datetime(),
  provider: z.string(),
  url: z.string().url(),
  api_key: z.string().optional(),
  input_cost: z.number().min(0),
  output_cost: z.number().min(0),
  min_cost_per_request: z
    .number()
    .min(0, 'Minimum cost per request must be non-negative')
    .default(0),
  min_cash_per_request: z
    .number()
    .min(0, 'Minimum cash per request must be non-negative')
    .default(0),
  contextLength: z.number().int().optional(),
  apiKeyRequired: z.boolean().default(true),
  provider_id: z.string().optional(),
  is_free: z.boolean().default(false),
  soft_deleted: z.boolean().optional(),
  // API key type indicators
  has_own_api_key: z.boolean(),
  api_key_type: z.string(), // "individual" or "group"
});

// Schema for a model with additional provider-specific settings
export const ModelWithSettingsSchema = ModelSchema.extend({
  settings: z.record(z.unknown()).optional(),
  pricing: z
    .object({
      inputCostPer1kTokens: z.number().optional(),
      outputCostPer1kTokens: z.number().optional(),
      unitCost: z.number().optional(),
    })
    .optional(),
});

// Schema for creating a new model
export const CreateModelSchema = ModelSchema.omit({
  id: true,
  createdAt: true,
  updatedAt: true,
  has_own_api_key: true,
  api_key_type: true,
}).extend({
  settings: z.record(z.unknown()).optional(),
});

// Schema for updating an existing model
export const UpdateModelSchema = ModelSchema.partial().omit({
  id: true,
  createdAt: true,
  updatedAt: true,
  has_own_api_key: true,
  api_key_type: true,
});

// Schema for manual model addition form
export const ManualModelSchema = z
  .object({
    name: z.string().min(1, 'Name is required'),
    full_name: z.string().optional(),
    input_cost: z.number().min(0, 'Input cost must be non-negative'),
    output_cost: z.number().min(0, 'Output cost must be non-negative'),
    provider: z.string().min(1, 'Provider is required'),
    modelType: z.enum(['text', 'embedding', 'image', 'audio', 'multimodal']),
    description: z
      .string()
      .optional()
      .transform((val) => (val === '' ? undefined : val)),
    contextLength: z
      .number()
      .int()
      .min(0)
      .optional()
      .transform((val) => (val === 0 ? undefined : val)),
  })
  .transform((data) => ({
    ...data,
  }));

// Schema for group settings
export const GroupSettingsSchema = z.object({
  provider: z.string().min(1, 'Provider is required'),
  group_api_key: z
    .string()
    .transform((val) => (val === '' ? undefined : val))
    .optional(),
  group_url: z
    .string()
    .transform((val) => (val === '' ? undefined : val))
    .pipe(z.string().url('Please enter a valid URL').optional())
    .optional(),
});

// Schema for listing models with pagination
export const ModelListResponseSchema = z.object({
  data: z.array(ModelSchema),
  pagination: z.object({
    total: z.number(),
    page: z.number(),
    pageSize: z.number(),
    totalPages: z.number(),
  }),
});

// Schema for model testing request
export const ModelTestRequestSchema = z.object({
  modelId: z.string(),
  input: z.string(),
  parameters: z.record(z.unknown()).optional(),
});

// Schema for model testing response
export const ModelTestResponseSchema = z.object({
  output: z.string(),
  usage: z
    .object({
      promptTokens: z.number().optional(),
      completionTokens: z.number().optional(),
      totalTokens: z.number().optional(),
    })
    .optional(),
  timings: z
    .object({
      totalMs: z.number(),
    })
    .optional(),
});

// Export types derived from the schemas
export type Model = z.infer<typeof ModelSchema>;
export type ModelWithSettings = z.infer<typeof ModelWithSettingsSchema>;
export type CreateModel = z.infer<typeof CreateModelSchema>;
export type UpdateModel = z.infer<typeof UpdateModelSchema>;
export type ModelListResponse = z.infer<typeof ModelListResponseSchema>;
export type ModelTestRequest = z.infer<typeof ModelTestRequestSchema>;
export type ModelTestResponse = z.infer<typeof ModelTestResponseSchema>;
export type ManualModel = z.infer<typeof ManualModelSchema>;
export type GroupSettings = z.infer<typeof GroupSettingsSchema>;
