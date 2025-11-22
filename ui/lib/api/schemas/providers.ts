import { z } from 'zod';

// Schema for provider
export const ProviderSchema = z.object({
  id: z.string(),
  name: z.string(),
  url: z.string().url(),
  iconUrl: z.string().optional(),
  apiKeyRequired: z.boolean().default(true),
  created_at: z.string().optional(),
  updated_at: z.string().optional(),
  description: z.string().optional(),
  isEnabled: z.boolean().default(true),
});

// Schema for creating a provider
export const CreateProviderSchema = ProviderSchema.omit({
  id: true,
  created_at: true,
  updated_at: true,
});

// Schema for updating a provider
export const UpdateProviderSchema = CreateProviderSchema.partial();

// Export types
export type Provider = z.infer<typeof ProviderSchema>;
export type CreateProvider = z.infer<typeof CreateProviderSchema>;
export type UpdateProvider = z.infer<typeof UpdateProviderSchema>;
