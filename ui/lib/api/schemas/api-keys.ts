import { z } from 'zod';

export const ApiKeySchema = z.object({
  id: z.string(),
  name: z.string(),
  key: z.string(),
  user_id: z.string(),
  organization_id: z.string(),
  last_used_at: z.string().optional(),
  expires_at: z.string().optional(),
  is_active: z.boolean().default(true),
  created_at: z.string(),
  updated_at: z.string().optional(),
});

export const CreateApiKeySchema = ApiKeySchema.omit({
  id: true,
  key: true,
  created_at: true,
  updated_at: true,
  last_used_at: true,
});

export const UpdateApiKeySchema = z.object({
  name: z.string().optional(),
  is_active: z.boolean().optional(),
  expires_at: z.string().optional(),
});

export type ApiKey = z.infer<typeof ApiKeySchema>;
export type CreateApiKey = z.infer<typeof CreateApiKeySchema>;
export type UpdateApiKey = z.infer<typeof UpdateApiKeySchema>;
