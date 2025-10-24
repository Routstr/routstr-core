import { z } from 'zod';

export const OrganizationSchema = z.object({
  id: z.string(),
  name: z.string(),
  slug: z.string().optional(),
  logo_url: z.string().optional(),
  domain: z.string().optional(),
  is_active: z.boolean().default(true),
  plan: z.string().optional().default('free'),
  created_at: z.string(),
  updated_at: z.string().optional(),
});

export const CreateOrganizationSchema = z.object({
  name: z.string(),
  slug: z.string().optional(),
  logo_url: z.string().optional(),
  domain: z.string().optional(),
  is_active: z.boolean().default(true),
  plan: z.string().optional().default('free'),
});

export const UpdateOrganizationSchema = CreateOrganizationSchema.partial();

export type Organization = z.infer<typeof OrganizationSchema>;
export type CreateOrganization = z.infer<typeof CreateOrganizationSchema>;
export type UpdateOrganization = z.infer<typeof UpdateOrganizationSchema>;
